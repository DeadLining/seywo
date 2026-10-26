#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]
mod session_graph;
fn main() {
    tauri::Builder::default()
        .invoke_handler(tauri::generate_handler![session_graph::read_session_graph, session_graph::manage_session_graph])
        .setup(|app| {
            tauri::async_runtime::spawn(session_graph::resume_session_graph(app.handle().clone()));
            Ok(())
        })
        .run(tauri::generate_context!())
        .expect("SessionGraph application failed");
}
