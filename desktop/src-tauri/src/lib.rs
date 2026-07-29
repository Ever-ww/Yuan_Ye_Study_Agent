use serde::{Deserialize, Serialize};
use std::path::PathBuf;
use std::time::Duration;
use tauri::Manager;
use tauri_plugin_shell::ShellExt;

#[derive(Serialize)]
struct GatewayConnection {
    base_url: String,
    token: String,
}

#[derive(Deserialize)]
struct GatewayInstance {
    port: u16,
}

fn agent_home() -> Result<PathBuf, String> {
    if let Ok(explicit) = std::env::var("YY_AGENT_HOME") {
        return Ok(PathBuf::from(explicit));
    }
    #[cfg(target_os = "windows")]
    {
        let base = std::env::var("LOCALAPPDATA").map_err(|_| "LOCALAPPDATA 未设置")?;
        return Ok(PathBuf::from(base).join("YuanYeAgent"));
    }
    #[cfg(target_os = "macos")]
    {
        let home = std::env::var("HOME").map_err(|_| "HOME 未设置")?;
        return Ok(PathBuf::from(home).join("Library/Application Support/YuanYeAgent"));
    }
    #[cfg(target_os = "linux")]
    {
        let base = std::env::var("XDG_DATA_HOME")
            .map(PathBuf::from)
            .unwrap_or_else(|_| PathBuf::from(std::env::var("HOME").unwrap_or_default()).join(".local/share"));
        return Ok(base.join("yuan-ye-agent"));
    }
    #[allow(unreachable_code)]
    Err("当前平台不受支持".into())
}

#[tauri::command]
async fn gateway_connection(app: tauri::AppHandle) -> Result<GatewayConnection, String> {
    let home = agent_home()?;
    let home_text = home.to_string_lossy().to_string();
    let output = app
        .shell()
        .sidecar("yy-agent")
        .map_err(|error| error.to_string())?
        .args(["gateway", "start"])
        .env("YY_AGENT_HOME", &home_text)
        .output()
        .await
        .map_err(|error| error.to_string())?;
    if !output.status.success() {
        return Err(String::from_utf8_lossy(&output.stderr).to_string());
    }
    let token_path = home.join(".yy/gateway/token");
    let instance_path = home.join(".yy/gateway/instance.json");
    for _ in 0..100 {
        if let (Ok(token), Ok(instance_text)) = (
            std::fs::read_to_string(&token_path),
            std::fs::read_to_string(&instance_path),
        ) {
            let token = token.trim().to_string();
            if let Ok(instance) = serde_json::from_str::<GatewayInstance>(&instance_text) {
                if !token.is_empty() {
                    return Ok(GatewayConnection {
                        base_url: format!("http://127.0.0.1:{}", instance.port),
                        token,
                    });
                }
            }
        }
        tokio::time::sleep(Duration::from_millis(100)).await;
    }
    Err(format!("Gateway 已启动但未生成凭据：{}", token_path.display()))
}

pub fn run() {
    tauri::Builder::default()
        .plugin(tauri_plugin_shell::init())
        .plugin(tauri_plugin_dialog::init())
        .plugin(tauri_plugin_notification::init())
        .invoke_handler(tauri::generate_handler![gateway_connection])
        .setup(|app| {
            if let Some(window) = app.get_webview_window("main") {
                let _ = window.set_focus();
            }
            Ok(())
        })
        .run(tauri::generate_context!())
        .expect("Yuan Ye desktop failed");
}
