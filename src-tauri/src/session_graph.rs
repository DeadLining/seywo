//! On-demand local SessionGraph reads. No HTTP daemon, provider, retry, or polling.
use serde::{Deserialize, Serialize};
use serde_json::{json, Value};
use std::{
    path::{Path, PathBuf},
    process::Stdio,
    time::Duration,
};
use tauri::Manager;
use tokio::{
    io::{AsyncReadExt, AsyncWriteExt},
    process::Command,
    sync::Semaphore,
};

const MAX_BYTES: usize = 8 * 1024 * 1024;
static READ_SLOTS: Semaphore = Semaphore::const_new(4);
static SETTINGS_SLOTS: Semaphore = Semaphore::const_new(1);

#[derive(Debug, Deserialize, Serialize)]
#[serde(rename_all = "snake_case")]
pub(crate) enum Operation {
    Projects,
    WorkMap,
    Status,
    Memory,
    Evidence,
    SummaryCandidates,
}

fn request(
    operation: &Operation,
    project: Option<&str>,
    id: Option<&str>,
    offset: Option<u64>,
) -> Result<Value, String> {
    if !matches!(operation, Operation::Projects)
        && !project.is_some_and(|p| p.len() == 20 && p.bytes().all(|c| c.is_ascii_hexdigit()))
    {
        return Err("缺少有效的项目标识".into());
    }
    if matches!(operation, Operation::Memory | Operation::Evidence)
        && !id.is_some_and(|s| {
            !s.is_empty()
                && s.len() <= 256
                && s.bytes()
                    .all(|c| c.is_ascii_alphanumeric() || c == b'_' || c == b'-')
        })
    {
        return Err("缺少有效的来源标识".into());
    }
    if offset.is_some_and(|n| !matches!(operation, Operation::Evidence | Operation::WorkMap) || n > 50_000_000) {
        return Err("原文偏移无效".into());
    }
    Ok(json!({"operation":operation,"project":project,"id":id,"offset":offset}))
}

fn runtime_home() -> Result<PathBuf, String> {
    Ok(
        PathBuf::from(std::env::var_os("HOME").ok_or("无法定位用户目录")?)
            .join("Library/Application Support/local-ai-gateway/sessiongraph"),
    )
}

// Private local credentials: atomic replacement, never Keychain or child-process IPC.
fn save_jev_key(home: &Path, key: &str) -> Result<(), String> {
    use std::io::Write;
    #[cfg(unix)]
    use std::os::unix::fs::{DirBuilderExt, OpenOptionsExt, PermissionsExt};
    let save = || -> std::io::Result<()> {
        std::fs::create_dir_all(home)?;
        let dir = home.join("credentials");
        let mut builder = std::fs::DirBuilder::new();
        #[cfg(unix)]
        builder.mode(0o700);
        match builder.create(&dir) {
            Ok(()) => (),
            Err(e) if e.kind() == std::io::ErrorKind::AlreadyExists => (),
            Err(e) => return Err(e),
        }
        let meta = std::fs::symlink_metadata(&dir)?;
        if !meta.is_dir() || meta.file_type().is_symlink() {
            return Err(std::io::Error::other("unsafe credential directory"));
        }
        #[cfg(unix)]
        std::fs::set_permissions(&dir, std::fs::Permissions::from_mode(0o700))?;
        let target = dir.join("jev.json");
        if let Ok(meta) = std::fs::symlink_metadata(&target) {
            if !meta.is_file() || meta.file_type().is_symlink() {
                return Err(std::io::Error::other("unsafe credential file"));
            }
        }
        let nonce = std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)
            .unwrap_or_default()
            .as_nanos();
        let temp = dir.join(format!(".jev-{}-{nonce}.tmp", std::process::id()));
        let mut options = std::fs::OpenOptions::new();
        options.write(true).create_new(true);
        #[cfg(unix)]
        options.mode(0o600).custom_flags(libc::O_NOFOLLOW);
        let mut file = options.open(&temp)?;
        let result = (|| {
            file.write_all(serde_json::json!({"api_key":key}).to_string().as_bytes())?;
            file.sync_all()?;
            std::fs::rename(&temp, &target)?;
            std::fs::File::open(&dir)?.sync_all()
        })();
        if result.is_err() {
            let _ = std::fs::remove_file(&temp);
        }
        result
    };
    save().map_err(|_| "保存 Jev 密钥失败，请检查 SessionGraph 本地配置目录权限".into())
}

fn local_command(source: &Path, home: &Path) -> Result<Command, String> {
    local_command_mode(source, home, "native-read")
}

fn local_command_mode(source: &Path, home: &Path, mode: &str) -> Result<Command, String> {
    let manifest = home.join("runtime.json");
    let python = if manifest.exists() {
        if std::fs::metadata(&manifest)
            .map_err(|_| "无法读取运行配置")?
            .len()
            > 16384
        {
            return Err("运行配置过大".into());
        }
        let runtime: Value =
            serde_json::from_slice(&std::fs::read(manifest).map_err(|_| "无法读取运行配置")?)
                .map_err(|_| "运行配置无效")?;
        PathBuf::from(runtime["python"].as_str().ok_or("未配置 Python 运行时")?)
    } else if mode == "native-settings" {
        [
            "/Library/Frameworks/Python.framework/Versions/3.14/bin/python3",
            "/Library/Frameworks/Python.framework/Versions/3.13/bin/python3",
            "/Library/Frameworks/Python.framework/Versions/3.12/bin/python3",
            "/Library/Frameworks/Python.framework/Versions/3.11/bin/python3",
            "/opt/homebrew/bin/python3",
            "/usr/local/bin/python3",
        ]
        .iter()
        .map(PathBuf::from)
        .find(|p| p.is_file())
        .ok_or("需要先安装 Python 3.11+ 和 mcp 依赖；不会自动下载软件")?
    } else {
        return Err("SessionGraph 尚未安装，请打开项目记忆设置安装".into());
    };
    if !python.is_absolute() || !python.is_file() || !source.join("entrypoint.py").is_file() {
        return Err("SessionGraph 运行时缺失；未启动任何服务".into());
    }
    let mut command = Command::new(python);
    command
        .args(["-B"])
        .arg(source.join("entrypoint.py"))
        .arg(mode)
        .env("SESSIONGRAPH_RUNTIME_HOME", home)
        .env("PYTHONDONTWRITEBYTECODE", "1");
    Ok(command)
}

#[derive(Debug, Deserialize, Serialize)]
#[serde(rename_all = "snake_case")]
pub(crate) enum SettingsAction {
    Status,
    SaveModel,
    SaveProvider,
    EnableGraph,
    DisableGraph,
    SaveJev,
    StoragePreview,
    StorageCompact,
    CleanNotices,
    ClearTasks,
    InstallHooks,
    InstallMcp,
    UninstallHooks,
    UninstallMcp,
}

#[derive(Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
pub(crate) struct SettingsRequest {
    action: SettingsAction,
    project: Option<String>,
    model: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    endpoint: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    api_key_env: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    api_key: Option<String>,
}

fn validate_settings(request: &SettingsRequest) -> Result<(), String> {
    if request
        .project
        .as_ref()
        .is_some_and(|p| p.len() != 20 || !p.bytes().all(|c| c.is_ascii_hexdigit()))
        || (matches!(
            request.action,
            SettingsAction::SaveModel
                | SettingsAction::SaveProvider
                | SettingsAction::InstallHooks
                | SettingsAction::InstallMcp
                | SettingsAction::EnableGraph
                | SettingsAction::DisableGraph
                | SettingsAction::SaveJev
                | SettingsAction::StoragePreview
                | SettingsAction::StorageCompact
                | SettingsAction::CleanNotices
                | SettingsAction::ClearTasks
        ) && request.project.is_none())
    {
        return Err("请选择已初始化的项目".into());
    }
    if matches!(request.action, SettingsAction::SaveModel | SettingsAction::SaveProvider) {
        if !request.model.as_ref().is_some_and(|m| {
            !m.trim().is_empty() && m.chars().count() <= 256 && !m.chars().any(char::is_control)
        }) {
            return Err("请选择有效的抽取模型".into());
        }
    } else if request.model.is_some() {
        return Err("此操作不接受模型字段".into());
    }
    if matches!(request.action, SettingsAction::SaveProvider) {
        if !request.endpoint.as_ref().is_some_and(|e| !e.is_empty() && e.len() <= 2048 && !e.chars().any(char::is_control)) {
            return Err("请输入有效的 Responses 端点".into());
        }
    } else if request.endpoint.is_some() || request.api_key_env.is_some() {
        return Err("此操作不接受端点字段".into());
    }
    if matches!(request.action, SettingsAction::SaveJev) || (matches!(request.action, SettingsAction::SaveProvider) && request.api_key.is_some()) {
        if !request.api_key.as_ref().is_some_and(|key| {
            !key.trim().is_empty() && key.len() <= 2048 && !key.chars().any(char::is_control)
        }) {
            return Err("请输入有效的 Jev API Key".into());
        }
    } else if request.api_key.is_some() {
        return Err("此操作不接受凭据字段".into());
    }
    Ok(())
}

#[tauri::command]
pub(crate) async fn manage_session_graph(
    app: tauri::AppHandle,
    mut request: SettingsRequest,
) -> Result<Value, String> {
    validate_settings(&request)?;
    let _slot = SETTINGS_SLOTS
        .try_acquire()
        .map_err(|_| "设置正在处理中，请稍后重试")?;
    let saving_jev = matches!(request.action, SettingsAction::SaveJev);
    if saving_jev {
        let key = request.api_key.take().ok_or("缺少 Jev 凭据")?;
        let home = runtime_home()?;
        tokio::task::spawn_blocking(move || save_jev_key(&home, key.trim()))
            .await
            .map_err(|_| "保存 Jev 凭据任务失败")??;
        request.action = SettingsAction::Status;
    }
    let source = if cfg!(debug_assertions) {
        PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("../runtime")
    } else {
        app.path()
            .resource_dir()
            .map_err(|_| "无法定位应用资源")?
            .join("sessiongraph")
    };
    let timeout = if matches!(request.action, SettingsAction::StorageCompact) { 300 } else { 30 };
    let mut result = read_process(
        local_command_mode(&source, &runtime_home()?, "native-settings")?,
        serde_json::to_value(request).map_err(|_| "无效设置请求")?,
        Duration::from_secs(timeout),
        64 * 1024,
    )
    .await?;
    if saving_jev {
        result["message"] =
            serde_json::json!("Jev 密钥已保存，后台将直接使用；仅保存在本机 SessionGraph 私有配置中。");
    }
    Ok(result)
}

fn read_phase_diagnostic(bytes: &[u8]) -> Option<&'static str> {
    std::str::from_utf8(bytes).ok()?.lines().rev().find_map(|line| {
        let value: Value = serde_json::from_str(line).ok()?;
        value.get("sg_read_phase")?.as_str().map(str::to_owned)
    }).and_then(|phase| {
        match phase.as_str() {
            "catalog" => Some("项目目录"),
            "database.open" => Some("打开数据库"),
            "summary.candidates" => Some("材料查询"),
            "summary.rows" => Some("材料证据索引分页"),
            "summary.payload" => Some("材料原文校验"),
            "summary.context_payload" => Some("历史目标原文校验"),
            "summary.relations" => Some("材料关系证据"),
            "summary.relation_payload" => Some("材料关系原文校验"),
            "summary.context" => Some("历史目标上下文"),
            "summary.coverage" => Some("材料覆盖统计"),
            "map.rows" => Some("事项分页"),
            "map.references" => Some("引用索引"),
            "map.relations" => Some("关系证据"),
            "map.diagnostics" => Some("图谱统计"),
            _ => None,
        }
    })
}

async fn read_process(
    mut command: Command,
    payload: Value,
    timeout: Duration,
    max_bytes: usize,
) -> Result<Value, String> {
    command
        .stdin(Stdio::piped())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .kill_on_drop(true);
    let mut child = command
        .spawn()
        .map_err(|_| "无法启动 SessionGraph 本地读取进程")?;
    let stderr = child.stderr.take().ok_or("读取诊断通道不可用")?;
    let diagnostics = tokio::spawn(async move {
        let mut bytes = Vec::new();
        let _ = stderr.take(8192).read_to_end(&mut bytes).await;
        bytes
    });
    let result = tokio::time::timeout(timeout, async {
        let mut stdin = child.stdin.take().ok_or("读取进程输入不可用")?;
        stdin
            .write_all(payload.to_string().as_bytes())
            .await
            .map_err(|_| "写入请求失败")?;
        drop(stdin);
        let mut body = Vec::new();
        child
            .stdout
            .take()
            .ok_or("读取进程输出不可用")?
            .take((max_bytes + 1) as u64)
            .read_to_end(&mut body)
            .await
            .map_err(|_| "读取响应失败")?;
        if body.len() > max_bytes {
            return Err("SessionGraph 响应超过读取上限；未截断证据".to_string());
        }
        let status = child.wait().await.map_err(|_| "等待读取进程失败")?;
        if !status.success() {
            let failure = serde_json::from_slice::<Value>(&body).ok();
            let code = failure.as_ref().and_then(|v| v.get("error")).and_then(Value::as_str);
            return Err(match code {
                Some("material_revision_changed") => "材料版本变化，请刷新后重试",
                Some("local_read_locked") => "SessionGraph 数据库正在被占用，请稍后刷新",
                Some("local_read_timeout") => "SessionGraph 本地读取超时；已停止读取进程",
                _ => "SessionGraph 本地读取失败，请检查所选项目的配置和数据库",
            }.into());
        }
        let data: Value =
            serde_json::from_slice(&body).map_err(|_| "SessionGraph 返回的 JSON 无效")?;
        if !data.is_object()
            || data.get("error").is_some()
            || data.get("model_calls").and_then(Value::as_u64) != Some(0)
        {
            return Err("SessionGraph 响应不符合本地接口契约".into());
        }
        Ok(data)
    })
    .await
    .unwrap_or_else(|_| Err("SessionGraph 本地读取超时；已停止读取进程".into()));
    if result.is_err() {
        let _ = child.kill().await;
        let _ = child.wait().await;
    }
    let bytes = diagnostics.await.unwrap_or_default();
    result.map_err(|error| match read_phase_diagnostic(&bytes) {
        Some(phase) => format!("{error}（阶段：{phase}）"),
        None => error,
    })
}

#[tauri::command]
pub(crate) async fn read_session_graph(
    app: tauri::AppHandle,
    operation: Operation,
    project: Option<String>,
    id: Option<String>,
    offset: Option<u64>,
    query: Option<String>,
    chat: Option<String>,
    materials: Option<Value>,
) -> Result<Value, String> {
    let mut payload = request(&operation, project.as_deref(), id.as_deref(), offset)?;
    for (key, value, cap) in [("query", query, 1000), ("chat", chat, 256)] {
        if let Some(value) = value {
            if !matches!(operation, Operation::WorkMap) || value.chars().count() > cap {
                return Err("搜索参数无效".into());
            }
            payload[key] = json!(value);
        }
    }
    if matches!(operation, Operation::SummaryCandidates) {
        let value = materials.ok_or("缺少材料查询范围")?;
        if !value.is_object() || value.to_string().len() > 1024 {
            return Err("材料查询参数无效".into());
        }
        payload["materials"] = value;
    } else if materials.is_some() {
        return Err("此操作不接受材料查询".into());
    }
    let _slot = READ_SLOTS
        .try_acquire()
        .map_err(|_| "SessionGraph 读取繁忙，请稍后刷新")?;
    let source = if cfg!(debug_assertions) {
        PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("../runtime")
    } else {
        app.path()
            .resource_dir()
            .map_err(|_| "无法定位应用资源")?
            .join("sessiongraph")
    };
    read_process(
        local_command(&source, &runtime_home()?)?,
        payload,
        Duration::from_secs(10),
        MAX_BYTES,
    )
    .await
}

#[cfg(test)]
mod tests {
    use super::*;
    const PROJECT: &str = "a217c2bc1da5c4913760";
    #[test]
    fn material_timeout_uses_latest_phase_not_database_open() {
        let phases = b"{\"sg_read_phase\":\"database.open\"}\n{\"sg_read_phase\":\"summary.coverage\"}\n";
        assert_eq!(read_phase_diagnostic(phases), Some("材料覆盖统计"));
        let unknown = b"{\"sg_read_phase\":\"database.open\"}\n{\"sg_read_phase\":\"future.phase\"}\n";
        assert_eq!(read_phase_diagnostic(unknown), None);
    }

    fn fixture(script: &str) -> Command {
        let mut cmd = Command::new("/bin/sh");
        cmd.args(["-c", script]);
        cmd
    }
    #[test]
    fn operations_are_allowlisted_and_project_scoped() {
        assert!(serde_json::from_str::<Operation>("\"extract\"").is_err());
        assert!(request(&Operation::WorkMap, None, None, None).is_err());
        assert!(request(&Operation::WorkMap, Some("../../x"), None, None).is_err());
        assert!(request(
            &Operation::Memory,
            Some(PROJECT),
            Some("e_1&project=other"),
            None
        )
        .is_err());
        assert!(request(&Operation::Memory, Some(PROJECT), Some("e_1"), Some(1)).is_err());
        assert!(request(
            &Operation::Evidence,
            Some(PROJECT),
            Some("e_1"),
            Some(50_000_001)
        )
        .is_err());
        assert_eq!(
            request(&Operation::Memory, Some(PROJECT), Some("wm_1"), None).unwrap()["operation"],
            "memory"
        );
    }
    #[test]
    fn validates_separate_settings_contract() {
        for payload in [
            json!({"action":"extract"}),
            json!({"action":"status","path":"/tmp"}),
        ] {
            assert!(serde_json::from_value::<SettingsRequest>(payload).is_err());
        }
        for payload in [
            json!({"action":"save_model","project":PROJECT,"model":""}),
            json!({"action":"save_model","project":PROJECT,"model":"a\nb"}),
            json!({"action":"install_hooks"}),
            json!({"action":"enable_graph"}),
            json!({"action":"storage_preview"}),
            json!({"action":"storage_compact"}),
            json!({"action":"clean_notices"}),
            json!({"action":"clear_tasks"}),
            json!({"action":"storage_compact","project":PROJECT,"model":"unexpected"}),
            json!({"action":"save_jev","project":PROJECT,"api_key":""}),
            json!({"action":"save_jev","project":PROJECT,"api_key":"a\nb"}),
            json!({"action":"status","api_key":"unexpected"}),
            json!({"action":"status","project":"../x"}),
            json!({"action":"install_mcp","project":PROJECT,"model":"x"}),
        ] {
            assert!(validate_settings(&serde_json::from_value(payload).unwrap()).is_err());
        }
        for payload in [
            json!({"action":"status"}),
            json!({"action":"uninstall_hooks"}),
            json!({"action":"uninstall_mcp"}),
            json!({"action":"save_model","project":PROJECT,"model":"new/model"}),
            json!({"action":"install_hooks","project":PROJECT}),
            json!({"action":"install_mcp","project":PROJECT}),
            json!({"action":"enable_graph","project":PROJECT}),
            json!({"action":"disable_graph","project":PROJECT}),
            json!({"action":"storage_preview","project":PROJECT}),
            json!({"action":"storage_compact","project":PROJECT}),
            json!({"action":"clean_notices","project":PROJECT}),
            json!({"action":"clear_tasks","project":PROJECT}),
            json!({"action":"save_jev","project":PROJECT,"api_key":"synthetic"}),
        ] {
            assert!(validate_settings(&serde_json::from_value(payload).unwrap()).is_ok());
        }
    }

    #[test]
    #[cfg(unix)]
    fn local_jev_credentials_are_private_atomic_and_reject_symlinks() {
        use std::os::unix::fs::{symlink, PermissionsExt};
        let nonce = std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)
            .unwrap()
            .as_nanos();
        let home = std::env::temp_dir().join(format!("gateway-jev-{}-{nonce}", std::process::id()));
        save_jev_key(&home, "synthetic-one").unwrap();
        let dir = home.join("credentials");
        let file = dir.join("jev.json");
        assert_eq!(
            std::fs::metadata(&dir).unwrap().permissions().mode() & 0o777,
            0o700
        );
        assert_eq!(
            std::fs::metadata(&file).unwrap().permissions().mode() & 0o777,
            0o600
        );
        save_jev_key(&home, "synthetic-two").unwrap();
        let saved: Value = serde_json::from_slice(&std::fs::read(&file).unwrap()).unwrap();
        assert_eq!(saved["api_key"], "synthetic-two");
        assert_eq!(std::fs::read_dir(&dir).unwrap().count(), 1);
        let untouched = home.join("untouched");
        std::fs::write(&untouched, "original").unwrap();
        std::fs::remove_file(&file).unwrap();
        symlink(&untouched, &file).unwrap();
        assert!(save_jev_key(&home, "synthetic-three").is_err());
        assert_eq!(std::fs::read_to_string(&untouched).unwrap(), "original");
        std::fs::remove_file(&file).unwrap();
        std::fs::remove_dir(&dir).unwrap();
        symlink(&home, &dir).unwrap();
        assert!(save_jev_key(&home, "synthetic-three").is_err());
        std::fs::remove_file(&dir).unwrap();
        std::fs::remove_dir_all(&home).unwrap();
    }

    #[test]
    fn phase_diagnostics_only_accept_allowlisted_names() {
        assert_eq!(read_phase_diagnostic(br#"{"sg_read_phase":"map.relations"}"#), Some("关系证据"));
        assert_eq!(read_phase_diagnostic(br#"{"sg_read_phase":"secret/path"}"#), None);
        assert_eq!(read_phase_diagnostic(b"arbitrary provider output"), None);
    }

    #[tokio::test]
    async fn one_shot_read_no_server() {
        let result = read_process(
            fixture("cat >/dev/null; printf '{\"model_calls\":0,\"projects\":[]}'"),
            json!({}),
            Duration::from_secs(2),
            1024,
        )
        .await;
        assert!(result.is_ok(), "{result:?}");
    }
    #[tokio::test]
    async fn rejects_invalid_contracts() {
        for body in [
            "not json",
            "[]",
            "{\"model_calls\":1}",
            "{\"model_calls\":0,\"error\":\"private\"}",
        ] {
            assert!(read_process(
                fixture(&format!("printf '%s' '{}'", body)),
                json!({}),
                Duration::from_secs(2),
                1024
            )
            .await
            .is_err());
        }
    }
    #[tokio::test]
    async fn timeout_and_oversize_stop_process_without_retry() {
        assert!(read_process(
            fixture("exec sleep 10"),
            json!({}),
            Duration::from_millis(40),
            1024
        )
        .await
        .unwrap_err()
        .contains("超时"));
        assert!(read_process(
            fixture("printf 'xxxxxxxxxxxxxxxx'"),
            json!({}),
            Duration::from_secs(2),
            8
        )
        .await
        .unwrap_err()
        .contains("上限"));
        assert!(
            read_process(fixture("exit 1"), json!({}), Duration::from_secs(2), 100)
                .await
                .is_err()
        );
    }
    #[tokio::test]
    #[ignore = "explicit read-only smoke; only the fruitfly project"]
    async fn live_fruitfly_read_only() {
        let source = PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("../runtime");
        for op in [Operation::WorkMap, Operation::Status] {
            let payload = request(&op, Some(PROJECT), None, None).unwrap();
            let data = read_process(
                local_command(&source, &runtime_home().unwrap()).unwrap(),
                payload,
                Duration::from_secs(10),
                MAX_BYTES,
            )
            .await
            .unwrap();
            assert_eq!(data["model_calls"], 0);
        }
    }
}

/// Restore the already-authorized durable queue once at app startup. No polling.
pub(crate) async fn resume_session_graph(app: tauri::AppHandle) {
    let Ok(home) = runtime_home() else { return };
    if !home.join("continuations/queue.json").is_file() {
        return;
    }
    let source = if cfg!(debug_assertions) {
        PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("../runtime")
    } else {
        let Ok(resources) = app.path().resource_dir() else {
            return;
        };
        resources.join("sessiongraph")
    };
    if let Ok(mut command) = local_command_mode(&source, &home, "continue-pending") {
        let _ = command
            .stdin(Stdio::null())
            .stdout(Stdio::null())
            .stderr(Stdio::null())
            .status()
            .await;
    }
}
