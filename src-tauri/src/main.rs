#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]
mod session_graph;
fn main() {
    tauri::Builder::default()
        .invoke_handler(tauri::generate_handler![session_graph::read_session_graph, session_graph::manage_session_graph, session_graph::generate_task_flow, session_graph::edit_task_node])
        .run(tauri::generate_context!())
        .expect("SessionGraph application failed");
}
