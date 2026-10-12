#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

#[cfg(feature = "packaged-daemon")]
mod daemon;

#[cfg(all(feature = "focus-test", feature = "packaged-daemon"))]
compile_error!("focus-test builds must not start the packaged transcription daemon");

use std::{
    fs,
    path::{Path, PathBuf},
    sync::{Arc, Mutex},
    time::{Duration, Instant},
};

use base64::{engine::general_purpose::URL_SAFE_NO_PAD, Engine as _};
use serde_json::json;
use sha2::{Digest, Sha256};
use tauri::{
    image::Image,
    menu::{Menu, MenuItem},
    tray::{MouseButton, MouseButtonState, TrayIcon, TrayIconBuilder, TrayIconEvent},
    webview::WebviewWindow,
    Emitter, LogicalSize, Manager, PhysicalPosition, RunEvent, State,
};

const PANEL_WIDTH: i32 = 420;
const PANEL_HEIGHT: i32 = 560;
const EXPANDED_PANEL_HEIGHT: f64 = 760.0;
const SCREEN_GAP: i32 = 8;
const UNINSTALL_ARGUMENT: &str = "--whisper-uninstall";

#[derive(Clone, Default)]
struct PanelAnchor(Arc<Mutex<(i32, i32)>>);

#[derive(Clone, Default)]
struct FocusBaseline(Arc<Mutex<Option<isize>>>);

#[derive(Clone)]
struct ShortcutStatus(String);

#[derive(Clone)]
struct WebsocketToken(String);

#[derive(Clone, Default)]
struct PendingAuthCallback(Arc<Mutex<Option<String>>>);

#[derive(Clone)]
struct OAuthClientState {
    api_url_path: Arc<PathBuf>,
    pending: Arc<Mutex<Option<(String, Instant)>>>,
}

const AUTH_KEYRING_SERVICE: &str = "tech.intejers.wicx";
const AUTH_KEYRING_ACCOUNT: &str = "account-session";

fn validated_api_origin(candidate: &str) -> Result<String, String> {
    let url = url::Url::parse(candidate.trim()).map_err(|_| {
        "Enter a valid HTTPS service URL, such as https://api.example.com".to_string()
    })?;
    let local_http = cfg!(debug_assertions)
        && url.scheme() == "http"
        && url
            .host_str()
            .is_some_and(|host| matches!(host, "localhost" | "127.0.0.1" | "[::1]" | "::1"));
    if (url.scheme() != "https" && !local_http)
        || url.host_str().is_none()
        || !url.username().is_empty()
        || url.password().is_some()
        || !matches!(url.path(), "" | "/")
        || url.query().is_some()
        || url.fragment().is_some()
    {
        return Err("Use only the service origin (HTTPS required; localhost HTTP is allowed in development).".to_string());
    }
    Ok(url.origin().ascii_serialization())
}

fn read_auth_api_url(path: &Path) -> Result<String, String> {
    match fs::read_to_string(path) {
        Ok(value) => validated_api_origin(&value),
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => Ok(String::new()),
        Err(_) => Err("Could not read the saved Wicx service URL.".to_string()),
    }
}

#[tauri::command]
fn get_auth_api_url(auth: State<'_, OAuthClientState>) -> Result<String, String> {
    read_auth_api_url(&auth.api_url_path)
}

#[tauri::command]
fn save_auth_api_url(auth: State<'_, OAuthClientState>, url: String) -> Result<String, String> {
    let origin = validated_api_origin(&url)?;
    if let Some(parent) = auth.api_url_path.parent() {
        fs::create_dir_all(parent)
            .map_err(|_| "Could not prepare Wicx settings storage.".to_string())?;
    }
    fs::write(auth.api_url_path.as_path(), &origin)
        .map_err(|_| "Could not save the Wicx service URL.".to_string())?;
    Ok(origin)
}

#[tauri::command]
async fn start_oauth(provider: String, auth: State<'_, OAuthClientState>) -> Result<(), String> {
    if !matches!(provider.as_str(), "google" | "apple") {
        return Err("Choose Google or Apple to sign in.".to_string());
    }
    let origin = read_auth_api_url(&auth.api_url_path)?;
    if origin.is_empty() {
        return Err("Save your Wicx service URL before signing in.".to_string());
    }
    let verifier = format!(
        "{}{}",
        uuid::Uuid::new_v4().simple(),
        uuid::Uuid::new_v4().simple()
    );
    let challenge = URL_SAFE_NO_PAD.encode(Sha256::digest(verifier.as_bytes()));
    let response = reqwest::Client::new()
        .post(format!("{origin}/v1/auth/oauth/{provider}/start"))
        .json(&json!({ "code_challenge": challenge }))
        .send()
        .await
        .map_err(|_| {
            "Could not reach the Wicx sign-in service. Check the URL and your connection."
                .to_string()
        })?;
    if !response.status().is_success() {
        return Err(format!(
            "The sign-in service could not start {provider} sign-in (HTTP {}).",
            response.status().as_u16()
        ));
    }
    let payload: serde_json::Value = response
        .json()
        .await
        .map_err(|_| "The sign-in service returned an invalid response.".to_string())?;
    let authorization_url = payload
        .get("authorization_url")
        .and_then(|value| value.as_str())
        .ok_or_else(|| "The sign-in service did not provide a sign-in link.".to_string())?;
    let parsed = url::Url::parse(authorization_url)
        .map_err(|_| "The sign-in service returned an invalid sign-in link.".to_string())?;
    if parsed.scheme() != "https" || parsed.host_str().is_none() {
        return Err("The sign-in service returned an insecure sign-in link.".to_string());
    }
    *auth
        .pending
        .lock()
        .map_err(|_| "Sign-in could not be prepared.".to_string())? =
        Some((verifier, Instant::now()));
    open::that(parsed.as_str())
        .map_err(|_| "Could not open your default browser for sign-in.".to_string())
}

#[tauri::command]
async fn complete_auth_callback(
    callback: State<'_, PendingAuthCallback>,
    auth: State<'_, OAuthClientState>,
) -> Result<bool, String> {
    let Some(code) = callback
        .0
        .lock()
        .map_err(|_| "Sign-in callback could not be read.".to_string())?
        .take()
    else {
        return Ok(false);
    };
    let pending = auth
        .pending
        .lock()
        .map_err(|_| "Sign-in state could not be read.".to_string())?
        .take();
    let Some((verifier, started)) = pending else {
        return Err(
            "No sign-in attempt is waiting for this callback. Try signing in again.".to_string(),
        );
    };
    if started.elapsed() > Duration::from_secs(180) {
        return Err("Sign-in took too long. Try signing in again.".to_string());
    }
    let origin = read_auth_api_url(&auth.api_url_path)?;
    if origin.is_empty() {
        return Err("The Wicx service URL is not configured.".to_string());
    }
    let response = reqwest::Client::new()
        .post(format!("{origin}/v1/auth/session/exchange"))
        .json(&json!({ "code": code, "code_verifier": verifier }))
        .send()
        .await
        .map_err(|_| "Could not complete sign-in with the Wicx service.".to_string())?;
    if !response.status().is_success() {
        return Err(format!(
            "The sign-in service rejected this session (HTTP {}). Try again.",
            response.status().as_u16()
        ));
    }
    let payload: serde_json::Value = response
        .json()
        .await
        .map_err(|_| "The sign-in service returned an invalid session.".to_string())?;
    let token = payload
        .get("access_token")
        .and_then(|value| value.as_str())
        .filter(|value| !value.is_empty())
        .ok_or_else(|| "The sign-in service did not return a session token.".to_string())?;
    if payload
        .get("token_type")
        .and_then(|value| value.as_str())
        .is_some_and(|kind| !kind.eq_ignore_ascii_case("bearer"))
    {
        return Err("The sign-in service returned an unsupported session type.".to_string());
    }
    keyring::Entry::new(AUTH_KEYRING_SERVICE, AUTH_KEYRING_ACCOUNT)
        .and_then(|entry| entry.set_password(token))
        .map_err(|_| "Sign-in succeeded, but Wicx could not store the session in your operating-system credential store.".to_string())?;
    Ok(true)
}

#[tauri::command]
fn auth_is_signed_in() -> bool {
    keyring::Entry::new(AUTH_KEYRING_SERVICE, AUTH_KEYRING_ACCOUNT)
        .and_then(|entry| entry.get_password())
        .is_ok_and(|token| !token.is_empty())
}

#[tauri::command]
fn sign_out() -> Result<(), String> {
    match keyring::Entry::new(AUTH_KEYRING_SERVICE, AUTH_KEYRING_ACCOUNT)
        .and_then(|entry| entry.delete_credential())
    {
        Ok(()) => Ok(()),
        Err(keyring::Error::NoEntry) => Ok(()),
        Err(_) => Err(
            "Could not remove the Wicx session from your operating-system credential store."
                .to_string(),
        ),
    }
}

fn public_billing_status(payload: serde_json::Value) -> Result<serde_json::Value, String> {
    let object = payload
        .as_object()
        .ok_or_else(|| "The billing service returned an invalid status.".to_string())?;
    let features = object
        .get("features")
        .and_then(serde_json::Value::as_object)
        .ok_or_else(|| "The billing service returned an invalid feature list.".to_string())?
        .iter()
        .filter_map(|(name, enabled)| {
            enabled
                .as_bool()
                .map(|value| (name.clone(), serde_json::Value::Bool(value)))
        })
        .collect::<serde_json::Map<_, _>>();
    let subscriptions = object
        .get("subscriptions")
        .and_then(serde_json::Value::as_array)
        .ok_or_else(|| "The billing service returned an invalid plan list.".to_string())?
        .iter()
        .filter_map(|subscription| {
            let source = subscription.as_object()?;
            let mut summary = serde_json::Map::new();
            for field in ["plan_id", "status", "current_period_end"] {
                if let Some(value) = source.get(field).and_then(serde_json::Value::as_str) {
                    summary.insert(
                        field.to_string(),
                        serde_json::Value::String(value.to_string()),
                    );
                }
            }
            Some(serde_json::Value::Object(summary))
        })
        .collect::<Vec<_>>();
    let usage = object
        .get("usage")
        .and_then(serde_json::Value::as_object)
        .map(|items| {
            items
                .iter()
                .filter_map(|(name, raw)| {
                    let source = raw.as_object()?;
                    let mut summary = serde_json::Map::new();
                    for field in ["plan_id", "period_end"] {
                        if let Some(value) = source.get(field).and_then(serde_json::Value::as_str) {
                            summary.insert(
                                field.to_string(),
                                serde_json::Value::String(value.to_string()),
                            );
                        }
                    }
                    for field in [
                        "limit_milliseconds",
                        "used_milliseconds",
                        "remaining_milliseconds",
                    ] {
                        if let Some(value) = source
                            .get(field)
                            .filter(|value| value.is_null() || value.as_u64().is_some())
                        {
                            summary.insert(field.to_string(), value.clone());
                        }
                    }
                    (!summary.is_empty())
                        .then(|| (name.clone(), serde_json::Value::Object(summary)))
                })
                .collect::<serde_json::Map<_, _>>()
        })
        .unwrap_or_default();
    Ok(serde_json::json!({
        "features": features,
        "subscriptions": subscriptions,
        "usage": usage,
    }))
}

fn public_billing_plans(payload: serde_json::Value) -> Result<serde_json::Value, String> {
    let plans = payload
        .get("plans")
        .and_then(serde_json::Value::as_array)
        .ok_or_else(|| "The Wicx service returned an invalid plan catalog.".to_string())?;
    let plans = plans
        .iter()
        .filter_map(|plan| {
            let source = plan.as_object()?;
            let plan_id = source.get("plan_id")?.as_str()?;
            let amount_minor = source.get("amount_minor")?.as_u64()?;
            let currency = source.get("currency")?.as_str()?;
            let billing_mode = source.get("billing_mode")?.as_str()?;
            if !matches!(billing_mode, "recurring" | "one_time")
                || currency.len() != 3
                || !currency.bytes().all(|byte| byte.is_ascii_uppercase())
            {
                return None;
            }
            let features = source
                .get("features")
                .and_then(serde_json::Value::as_array)
                .map(|values| {
                    values
                        .iter()
                        .filter_map(serde_json::Value::as_str)
                        .map(str::to_string)
                        .collect::<Vec<_>>()
                })
                .unwrap_or_default();
            let mut safe_plan = serde_json::json!({
                "plan_id": plan_id,
                "amount_minor": amount_minor,
                "currency": currency,
                "billing_mode": billing_mode,
                "features": features,
            });
            if let Some(cloud_minutes) = source
                .get("cloud_minutes")
                .and_then(serde_json::Value::as_u64)
            {
                safe_plan["cloud_minutes"] = serde_json::json!(cloud_minutes);
            }
            if let Some(period_days) = source
                .get("period_days")
                .and_then(serde_json::Value::as_u64)
            {
                safe_plan["period_days"] = serde_json::json!(period_days);
            }
            if let Some(renewal) = source.get("renewal").and_then(serde_json::Value::as_str) {
                safe_plan["renewal"] = serde_json::Value::String(renewal.to_string());
            }
            if let Some(channels) = source.get("channels").and_then(serde_json::Value::as_array) {
                safe_plan["channels"] = serde_json::Value::Array(
                    channels
                        .iter()
                        .filter_map(serde_json::Value::as_str)
                        .map(|channel| serde_json::Value::String(channel.to_string()))
                        .collect(),
                );
            }
            Some(safe_plan)
        })
        .collect::<Vec<_>>();
    Ok(serde_json::json!({ "plans": plans }))
}

#[tauri::command]
async fn fetch_billing_plans(
    auth: State<'_, OAuthClientState>,
) -> Result<serde_json::Value, String> {
    let origin = read_auth_api_url(&auth.api_url_path)?;
    if origin.is_empty() {
        return Err("Save your Wicx service URL before viewing plans.".to_string());
    }
    let response = reqwest::Client::new()
        .get(format!("{origin}/v1/billing/plans"))
        .send()
        .await
        .map_err(|_| "Could not reach the Wicx plan catalog.".to_string())?;
    if response.status().as_u16() == 404 {
        return Err("Plans are not configured on this Wicx service yet.".to_string());
    }
    if !response.status().is_success() {
        return Err(format!(
            "Wicx could not load its plans (HTTP {}).",
            response.status().as_u16()
        ));
    }
    let payload = response
        .json::<serde_json::Value>()
        .await
        .map_err(|_| "The Wicx service returned an invalid plan catalog.".to_string())?;
    public_billing_plans(payload)
}

#[tauri::command]
async fn create_checkout(
    plan_id: String,
    email: String,
    auth: State<'_, OAuthClientState>,
) -> Result<(), String> {
    let origin = read_auth_api_url(&auth.api_url_path)?;
    if origin.is_empty() {
        return Err("Save your Wicx service URL before starting checkout.".to_string());
    }
    let token = keyring::Entry::new(AUTH_KEYRING_SERVICE, AUTH_KEYRING_ACCOUNT)
        .and_then(|entry| entry.get_password())
        .map_err(|_| "Sign in to purchase a subscription.".to_string())?;
    let response = reqwest::Client::new()
        .post(format!("{origin}/v1/billing/checkout"))
        .bearer_auth(token)
        .json(&serde_json::json!({ "plan_id": plan_id, "email": email.trim() }))
        .send()
        .await
        .map_err(|_| "Could not reach the Wicx checkout service.".to_string())?;
    match response.status().as_u16() {
        401 => return Err("Your Wicx session has expired. Sign in again.".to_string()),
        503 => return Err("Checkout is not configured on this Wicx service yet.".to_string()),
        status if !response.status().is_success() => {
            return Err(format!(
                "Checkout could not start (HTTP {status}). Check the billing email and try again."
            ));
        }
        _ => {}
    }
    let payload = response
        .json::<serde_json::Value>()
        .await
        .map_err(|_| "The checkout service returned an invalid response.".to_string())?;
    let checkout_url = payload
        .get("authorization_url")
        .and_then(serde_json::Value::as_str)
        .ok_or_else(|| "The checkout service did not return a payment link.".to_string())?;
    let parsed = url::Url::parse(checkout_url)
        .map_err(|_| "The checkout service returned an invalid payment link.".to_string())?;
    if parsed.scheme() != "https" || parsed.host_str() != Some("checkout.paystack.com") {
        return Err("The checkout service returned an untrusted payment link.".to_string());
    }
    open::that(parsed.as_str())
        .map_err(|_| "Could not open the Paystack checkout in your browser.".to_string())
}

fn trusted_management_url(candidate: &str) -> bool {
    let Ok(url) = url::Url::parse(candidate) else {
        return false;
    };
    url.scheme() == "https"
        && url.host_str() == Some("paystack.com")
        && url.path().starts_with("/manage/subscriptions/")
        && url.query().is_some_and(|query| !query.is_empty())
        && url.username().is_empty()
        && url.password().is_none()
}

#[tauri::command]
async fn manage_subscription(
    plan_id: String,
    auth: State<'_, OAuthClientState>,
) -> Result<(), String> {
    let origin = read_auth_api_url(&auth.api_url_path)?;
    if origin.is_empty() {
        return Err("Save your Wicx service URL before managing a subscription.".to_string());
    }
    let token = keyring::Entry::new(AUTH_KEYRING_SERVICE, AUTH_KEYRING_ACCOUNT)
        .and_then(|entry| entry.get_password())
        .map_err(|_| "Sign in to manage your subscription.".to_string())?;
    let response = reqwest::Client::new()
        .post(format!("{origin}/v1/billing/manage"))
        .bearer_auth(token)
        .json(&serde_json::json!({ "plan_id": plan_id }))
        .send()
        .await
        .map_err(|_| "Could not reach the Wicx subscription service.".to_string())?;
    match response.status().as_u16() {
        401 => return Err("Your Wicx session has expired. Sign in again.".to_string()),
        404 => return Err("No active recurring subscription was found for this plan.".to_string()),
        409 => {
            return Err(
                "This plan renews manually and has no automatic subscription to manage."
                    .to_string(),
            )
        }
        503 => {
            return Err(
                "Subscription management is not configured on this Wicx service yet.".to_string(),
            )
        }
        status if !response.status().is_success() => {
            return Err(format!(
                "Wicx could not prepare subscription management (HTTP {status})."
            ));
        }
        _ => {}
    }
    let payload = response
        .json::<serde_json::Value>()
        .await
        .map_err(|_| "The subscription service returned an invalid response.".to_string())?;
    let management_url = payload
        .get("management_url")
        .and_then(serde_json::Value::as_str)
        .ok_or_else(|| "The subscription service did not return a management link.".to_string())?;
    if !trusted_management_url(management_url) {
        return Err("The subscription service returned an untrusted management link.".to_string());
    }
    open::that(management_url)
        .map_err(|_| "Could not open subscription management in your browser.".to_string())
}

#[tauri::command]
async fn fetch_billing_status(
    auth: State<'_, OAuthClientState>,
) -> Result<serde_json::Value, String> {
    let origin = read_auth_api_url(&auth.api_url_path)?;
    if origin.is_empty() {
        return Err("Save your Wicx service URL before checking subscription access.".to_string());
    }
    let token = keyring::Entry::new(AUTH_KEYRING_SERVICE, AUTH_KEYRING_ACCOUNT)
        .and_then(|entry| entry.get_password())
        .map_err(|_| "Sign in to check your subscription access.".to_string())?;
    let response = reqwest::Client::new()
        .get(format!("{origin}/v1/billing/status"))
        .bearer_auth(token)
        .send()
        .await
        .map_err(|_| "Could not reach the Wicx subscription service.".to_string())?;
    match response.status().as_u16() {
        401 => return Err("Your Wicx session has expired. Sign in again.".to_string()),
        404 => {
            return Err("Subscription status is not enabled on this Wicx service yet.".to_string())
        }
        status if !response.status().is_success() => {
            return Err(format!(
                "Wicx could not check subscription access (HTTP {status})."
            ));
        }
        _ => {}
    }
    let payload = response
        .json::<serde_json::Value>()
        .await
        .map_err(|_| "The Wicx subscription service returned an invalid response.".to_string())?;
    public_billing_status(payload)
}

/// Rewrite dictation through the configured Wicx service. The account token
/// remains inside the native shell and is never exposed to the WebView or
/// transcription sidecar.
#[tauri::command]
async fn rewrite_transcript(
    text: String,
    auth: State<'_, OAuthClientState>,
) -> Result<String, String> {
    let text = text.trim();
    if text.is_empty() || text.len() > 20_000 {
        return Err("There is no transcript to rewrite, or it is too long.".to_string());
    }
    let origin = read_auth_api_url(&auth.api_url_path)?;
    if origin.is_empty() {
        return Err("Set your Wicx service URL before using rewrite.".to_string());
    }
    let token = keyring::Entry::new(AUTH_KEYRING_SERVICE, AUTH_KEYRING_ACCOUNT)
        .and_then(|entry| entry.get_password())
        .map_err(|_| "Sign in to use cloud rewriting.".to_string())?;
    let response = reqwest::Client::new()
        .post(format!("{origin}/v1/rewrite"))
        .bearer_auth(token)
        .json(&serde_json::json!({ "text": text }))
        .send()
        .await
        .map_err(|_| "Could not reach Wicx rewrite. Check your internet connection.".to_string())?;
    match response.status().as_u16() {
        401 => return Err("Your Wicx session has expired. Sign in again.".to_string()),
        402 | 403 => return Err("Cloud rewriting requires an active subscription.".to_string()),
        404 | 503 => {
            return Err("Cloud rewriting is not available on this Wicx service yet.".to_string())
        }
        status if !response.status().is_success() => {
            return Err(format!(
                "Wicx could not rewrite this transcript (HTTP {status})."
            ));
        }
        _ => {}
    }
    let payload = response
        .json::<serde_json::Value>()
        .await
        .map_err(|_| "Wicx rewrite returned an invalid response.".to_string())?;
    payload
        .get("text")
        .and_then(serde_json::Value::as_str)
        .filter(|rewritten| !rewritten.trim().is_empty())
        .map(str::to_string)
        .ok_or_else(|| "Wicx rewrite returned empty text.".to_string())
}

fn auth_handoff_code(candidate: &str) -> Option<String> {
    let url = url::Url::parse(candidate).ok()?;
    if url.scheme() != "wicx"
        || url.host_str() != Some("auth")
        || url.path() != "/callback"
        || url.fragment().is_some()
        || url.username() != ""
        || url.password().is_some()
    {
        return None;
    }
    let mut codes = url.query_pairs().filter(|(key, _)| key == "code");
    let code = codes.next()?.1.into_owned();
    if codes.next().is_some()
        || code.len() < 32
        || code.len() > 512
        || !code
            .bytes()
            .all(|byte| byte.is_ascii_alphanumeric() || matches!(byte, b'-' | b'_'))
    {
        return None;
    }
    Some(code)
}

fn store_auth_callback(state: &PendingAuthCallback, candidate: &str) -> bool {
    let Some(code) = auth_handoff_code(candidate) else {
        return false;
    };
    if let Ok(mut pending) = state.0.lock() {
        *pending = Some(code);
        true
    } else {
        false
    }
}

fn mic_tray_icon(color: [u8; 4]) -> Image<'static> {
    let size = 32_u32;
    let mut rgba = vec![0_u8; (size * size * 4) as usize];
    for y in 0..size {
        for x in 0..size {
            let dx = x as i32 - 16;
            let dy = y as i32 - 16;
            if dx * dx + dy * dy <= 225 {
                let index = ((y * size + x) * 4) as usize;
                rgba[index..index + 4].copy_from_slice(&color);
                let mic = (13..=18).contains(&x) && (8..=19).contains(&y);
                let cradle = (10..=21).contains(&x)
                    && (19..=23).contains(&y)
                    && ((y == 19 && (11..=20).contains(&x))
                        || (y == 20 && (10..=21).contains(&x))
                        || (y == 21 && (11..=20).contains(&x))
                        || (y == 22 && (13..=18).contains(&x))
                        || (y == 23 && (15..=16).contains(&x)));
                if mic || cradle {
                    rgba[index..index + 4].copy_from_slice(&[255, 255, 255, 255]);
                }
            }
        }
    }
    Image::new_owned(rgba, size, size)
}

#[tauri::command]
fn set_tray_state(tray: State<'_, TrayIcon>, state: String) -> Result<(), String> {
    let color = match state.as_str() {
        "recording" | "error" => [232, 72, 72, 255],
        "processing" => [40, 120, 240, 255],
        _ => [132, 135, 145, 255],
    };
    tray.set_icon(Some(mic_tray_icon(color)))
        .map_err(|error| error.to_string())
}

#[tauri::command]
fn focus_test_build() -> bool {
    cfg!(feature = "focus-test")
}

#[tauri::command]
fn recording_shortcut_status(status: State<'_, ShortcutStatus>) -> String {
    status.0.clone()
}

#[tauri::command]
fn websocket_token(token: State<'_, WebsocketToken>) -> String {
    token.0.clone()
}

fn load_or_create_websocket_token(path: &Path) -> Result<String, String> {
    match fs::read_to_string(path) {
        Ok(value) => {
            if let Ok(token) = uuid::Uuid::parse_str(value.trim()) {
                return Ok(token.to_string());
            }
        }
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => {}
        Err(error) => {
            return Err(format!(
                "Could not read Wicx's local connection token: {error}"
            ));
        }
    }

    if let Some(parent) = path.parent() {
        fs::create_dir_all(parent).map_err(|error| {
            format!("Could not prepare Wicx's local settings folder: {error}")
        })?;
    }
    let token = uuid::Uuid::new_v4().to_string();
    fs::write(path, &token)
        .map_err(|error| format!("Could not save Wicx's local connection token: {error}"))?;
    Ok(token)
}

#[tauri::command]
fn show_key_settings(app: tauri::AppHandle) -> Result<(), String> {
    let window = app
        .get_webview_window("keys")
        .ok_or_else(|| "Wicx key settings are unavailable".to_string())?;
    window.show().map_err(|error| error.to_string())?;
    window.set_focus().map_err(|error| error.to_string())
}

#[tauri::command]
fn open_diagnostics_folder() -> Result<(), String> {
    #[cfg(windows)]
    {
        let local_app_data = std::env::var_os("LOCALAPPDATA")
            .ok_or_else(|| "Windows did not provide the local app-data folder".to_string())?;
        let log_directory = Path::new(&local_app_data).join("Whisper").join("logs");
        fs::create_dir_all(&log_directory)
            .map_err(|error| format!("Could not create Wicx's diagnostics folder: {error}"))?;
        std::process::Command::new("explorer.exe")
            .arg(log_directory)
            .spawn()
            .map_err(|error| format!("Could not open Wicx's diagnostics folder: {error}"))?;
        Ok(())
    }
    #[cfg(not(windows))]
    {
        Err("Wicx's diagnostics folder is only available on Windows".to_string())
    }
}

#[cfg(windows)]
fn prepare_non_activating_window(window: &WebviewWindow) -> Result<(), Box<dyn std::error::Error>> {
    use windows::Win32::UI::WindowsAndMessaging::{
        GetWindowLongPtrW, SetWindowLongPtrW, SetWindowPos, GWL_EXSTYLE, HWND_TOPMOST,
        SWP_FRAMECHANGED, SWP_NOACTIVATE, SWP_NOMOVE, SWP_NOSIZE, WS_EX_NOACTIVATE,
        WS_EX_TOOLWINDOW,
    };

    let hwnd = window.hwnd()?;
    let required = WS_EX_NOACTIVATE.0 as isize | WS_EX_TOOLWINDOW.0 as isize;
    unsafe {
        let current = GetWindowLongPtrW(hwnd, GWL_EXSTYLE);
        SetWindowLongPtrW(hwnd, GWL_EXSTYLE, current | required);
    }
    window.set_focusable(false)?;
    unsafe {
        SetWindowPos(
            hwnd,
            Some(HWND_TOPMOST),
            0,
            0,
            0,
            0,
            SWP_FRAMECHANGED | SWP_NOACTIVATE | SWP_NOMOVE | SWP_NOSIZE,
        )
        .ok()
        .ok_or_else(std::io::Error::last_os_error)?;
    }
    let actual = unsafe { GetWindowLongPtrW(hwnd, GWL_EXSTYLE) };
    if actual & required != required {
        return Err(
            std::io::Error::other("Windows did not apply the non-activating panel styles").into(),
        );
    }
    Ok(())
}

#[cfg(not(windows))]
fn prepare_non_activating_window(
    _window: &WebviewWindow,
) -> Result<(), Box<dyn std::error::Error>> {
    Ok(())
}

#[cfg(windows)]
fn apply_panel_backdrop(window: &WebviewWindow) {
    if window_vibrancy::apply_mica(window, Some(true)).is_err() {
        // Mica is Windows 11-only. Use the supported blur backdrop on older
        // Windows versions instead of failing to open the panel.
        let _ = window_vibrancy::apply_blur(window, Some((32, 33, 39, 230)));
    }
}

#[cfg(not(windows))]
fn apply_panel_backdrop(_window: &WebviewWindow) {}

fn panel_position(
    monitor: (i32, i32, i32, i32),
    tray: (i32, i32),
    panel: (i32, i32),
    gap: i32,
) -> (i32, i32) {
    let (origin_x, origin_y, monitor_width, monitor_height) = monitor;
    let (tray_x, tray_y) = tray;
    let (panel_width, panel_height) = panel;
    let min_x = origin_x + gap;
    let min_y = origin_y + gap;
    let max_x = (origin_x + monitor_width - panel_width - gap).max(min_x);
    let max_y = (origin_y + monitor_height - panel_height - gap).max(min_y);
    (
        (tray_x - panel_width / 2).clamp(min_x, max_x),
        (tray_y - panel_height - gap).clamp(min_y, max_y),
    )
}

fn panel_height_for_monitor(requested: f64, monitor_height: u32, scale: f64, gap: i32) -> f64 {
    let scale = if scale.is_finite() && scale > 0.0 {
        scale
    } else {
        1.0
    };
    let usable_height = (monitor_height as f64 - 2.0 * gap as f64) / scale;
    requested.min(usable_height.max(1.0))
}

fn show_panel(
    window: &WebviewWindow,
    baseline: &FocusBaseline,
    tray_x: i32,
    tray_y: i32,
    panel_height: f64,
) {
    let foreground_before = foreground_window();
    if let Ok(mut saved) = baseline.0.lock() {
        *saved = foreground_before;
    }
    let Ok(monitors) = window.available_monitors() else {
        let _ = window.set_size(LogicalSize::new(PANEL_WIDTH as f64, panel_height));
        present_panel(
            window,
            tray_x - PANEL_WIDTH / 2,
            tray_y - panel_height.round() as i32 - SCREEN_GAP,
        );
        emit_focus_check(window, foreground_before);
        let _ = window.emit("panel-shown", ());
        return;
    };
    let monitor = monitors.iter().find(|monitor| {
        let position = monitor.position();
        let size = monitor.size();
        tray_x >= position.x
            && tray_x < position.x + size.width as i32
            && tray_y >= position.y
            && tray_y < position.y + size.height as i32
    });

    if let Some(monitor) = monitor {
        let origin = monitor.position();
        let size = monitor.size();
        let scale = monitor.scale_factor();
        let width = (PANEL_WIDTH as f64 * scale).round() as i32;
        let logical_height = panel_height_for_monitor(panel_height, size.height, scale, SCREEN_GAP);
        let height = (logical_height * scale).round() as i32;
        let _ = window.set_size(LogicalSize::new(PANEL_WIDTH as f64, logical_height));
        let (x, y) = panel_position(
            (origin.x, origin.y, size.width as i32, size.height as i32),
            (tray_x, tray_y),
            (width, height),
            SCREEN_GAP,
        );
        present_panel(window, x, y);
    } else {
        let _ = window.set_size(LogicalSize::new(PANEL_WIDTH as f64, panel_height));
        present_panel(
            window,
            tray_x - PANEL_WIDTH / 2,
            tray_y - panel_height.round() as i32 - SCREEN_GAP,
        );
    }
    emit_focus_check(window, foreground_before);
}

#[cfg(windows)]
fn monitor_foreground(window: WebviewWindow, baseline: FocusBaseline) {
    std::thread::spawn(move || {
        let mut escape_was_down = false;
        loop {
            std::thread::sleep(std::time::Duration::from_millis(100));
            if !window.is_visible().unwrap_or(false) {
                escape_was_down = false;
                continue;
            }

            let escape_down = escape_is_down();
            let escape_pressed = escape_down && !escape_was_down;
            escape_was_down = escape_down;

            let expected = baseline.0.lock().ok().and_then(|saved| *saved);
            if escape_pressed || (expected.is_some() && foreground_window() != expected) {
                let _ = window.hide();
                if let Ok(mut saved) = baseline.0.lock() {
                    *saved = None;
                }
            }
        }
    });
}

#[cfg(not(windows))]
fn monitor_foreground(_window: WebviewWindow, _baseline: FocusBaseline) {}

#[cfg(windows)]
fn escape_is_down() -> bool {
    use windows::Win32::UI::Input::KeyboardAndMouse::{GetAsyncKeyState, VK_ESCAPE};

    unsafe { GetAsyncKeyState(VK_ESCAPE.0 as i32) < 0 }
}

#[cfg(windows)]
fn foreground_window() -> Option<isize> {
    use windows::Win32::UI::WindowsAndMessaging::GetForegroundWindow;

    let handle = unsafe { GetForegroundWindow() };
    let raw = handle.0 as isize;
    (raw != 0).then_some(raw)
}

#[cfg(not(windows))]
fn foreground_window() -> Option<isize> {
    None
}

fn emit_focus_check(window: &WebviewWindow, foreground_before: Option<isize>) {
    #[cfg(windows)]
    {
        let window = window.clone();
        std::thread::spawn(move || {
            std::thread::sleep(std::time::Duration::from_millis(200));
            let visible = window.is_visible().unwrap_or(false);
            let preserved =
                visible && foreground_before.is_some() && foreground_before == foreground_window();
            let _ = window.emit("focus-check", preserved);
        });
    }
    #[cfg(not(windows))]
    {
        let _ = window.emit("focus-check", false);
    }
}

#[tauri::command]
fn resize_panel(
    app: tauri::AppHandle,
    anchor: State<'_, PanelAnchor>,
    expanded: bool,
) -> Result<(), String> {
    let window = app
        .get_webview_window("main")
        .ok_or_else(|| "Wicx panel is unavailable".to_string())?;
    let (tray_x, tray_y) = *anchor
        .0
        .lock()
        .map_err(|_| "Wicx panel position is unavailable".to_string())?;
    let height = if expanded {
        EXPANDED_PANEL_HEIGHT
    } else {
        PANEL_HEIGHT as f64
    };
    let baseline = app.state::<FocusBaseline>();
    show_panel(&window, &baseline, tray_x, tray_y, height);
    Ok(())
}

#[cfg(windows)]
fn present_panel(window: &WebviewWindow, x: i32, y: i32) {
    use windows::Win32::UI::WindowsAndMessaging::{
        SetWindowPos, HWND_TOPMOST, SWP_NOACTIVATE, SWP_NOSIZE, SWP_SHOWWINDOW,
    };

    let _ = window.set_position(PhysicalPosition::new(x, y));
    if let Ok(hwnd) = window.hwnd() {
        unsafe {
            let _ = SetWindowPos(
                hwnd,
                Some(HWND_TOPMOST),
                x,
                y,
                0,
                0,
                SWP_NOACTIVATE | SWP_NOSIZE | SWP_SHOWWINDOW,
            );
        }
    }
}

#[cfg(not(windows))]
fn present_panel(window: &WebviewWindow, x: i32, y: i32) {
    let _ = window.set_position(PhysicalPosition::new(x, y));
    let _ = window.show();
}

#[cfg(windows)]
fn cursor_position() -> Option<(i32, i32)> {
    use windows::{Win32::Foundation::POINT, Win32::UI::WindowsAndMessaging::GetCursorPos};

    let mut point = POINT::default();
    unsafe { GetCursorPos(&mut point).ok()? };
    Some((point.x, point.y))
}

#[cfg(not(windows))]
fn cursor_position() -> Option<(i32, i32)> {
    None
}

fn toggle_panel(app: &tauri::AppHandle) {
    let Some(window) = app.get_webview_window("main") else {
        return;
    };
    if window.is_visible().unwrap_or(false) {
        let _ = window.hide();
        return;
    }

    let (x, y) = cursor_position().unwrap_or((PANEL_WIDTH, PANEL_HEIGHT));
    if let Ok(mut anchor) = app.state::<PanelAnchor>().0.lock() {
        *anchor = (x, y);
    }
    let baseline = app.state::<FocusBaseline>();
    show_panel(&window, &baseline, x, y, PANEL_HEIGHT as f64);
    let _ = window.emit("panel-shown", ());
}

fn main() {
    #[cfg(feature = "packaged-daemon")]
    let builder = {
        use tauri_plugin_global_shortcut::{Code, Modifiers, Shortcut, ShortcutState};

        let shortcut = Shortcut::new(Some(Modifiers::CONTROL), Code::Space);
        let handler_shortcut = shortcut.clone();
        tauri::Builder::default()
            .plugin(tauri_plugin_single_instance::init(|app, args, _cwd| {
                if args.iter().any(|argument| argument == UNINSTALL_ARGUMENT) {
                    // Stop the supervised sidecar here, not only in
                    // RunEvent::Exit: exiting from inside this callback does
                    // not reliably deliver the Exit event first, which used
                    // to orphan a running daemon after uninstall.
                    #[cfg(feature = "packaged-daemon")]
                    if let Some(supervisor) = app.try_state::<daemon::DaemonSupervisor>() {
                        supervisor.request_shutdown();
                    }
                    app.exit(0);
                } else if !args
                    .iter()
                    .any(|argument| auth_handoff_code(argument).is_some())
                {
                    toggle_panel(app);
                }
            }))
            .plugin(tauri_plugin_deep_link::init())
            .plugin(tauri_plugin_shell::init())
            .plugin(tauri_plugin_clipboard_manager::init())
            .plugin(
                tauri_plugin_global_shortcut::Builder::new()
                    .with_handler(move |app, pressed, event| {
                        if pressed == &handler_shortcut && event.state() == ShortcutState::Pressed {
                            let _ = app.emit("recording-hotkey", ());
                        }
                    })
                    .build(),
            )
    };
    #[cfg(not(feature = "packaged-daemon"))]
    let builder = tauri::Builder::default()
        .plugin(tauri_plugin_deep_link::init())
        .plugin(tauri_plugin_shell::init())
        .plugin(tauri_plugin_clipboard_manager::init());
    builder
        .setup(move |app| {
            use tauri_plugin_deep_link::DeepLinkExt;

            if std::env::args().any(|argument| argument == UNINSTALL_ARGUMENT) {
                // Uninstall helper (NSIS PREUNINSTALL ExecWait). Exit
                // immediately without initializing windows, tray, or daemon so
                // the uninstaller never waits on a locked executable. The
                // resident instance (if any) is signaled separately via the
                // single-instance plugin in packaged builds, or force-closed
                // by the smoke test in focus-test builds.
                std::process::exit(0);
            }
            let pending_auth = PendingAuthCallback::default();
            app.manage(pending_auth.clone());
            let auth_api_url_path = app
                .path()
                .app_config_dir()
                .map_err(std::io::Error::other)?
                .join("auth-api-url");
            app.manage(OAuthClientState {
                api_url_path: Arc::new(auth_api_url_path),
                pending: Arc::new(Mutex::new(None)),
            });
            let auth_app = app.handle().clone();
            if let Some(urls) = app.deep_link().get_current()? {
                for url in urls {
                    if store_auth_callback(&pending_auth, url.as_str()) {
                        let _ = auth_app.emit("auth-callback-received", ());
                    }
                }
            }
            let auth_app = app.handle().clone();
            let auth_state = pending_auth.clone();
            app.deep_link().on_open_url(move |event| {
                for url in event.urls() {
                    if store_auth_callback(&auth_state, url.as_str()) {
                        let _ = auth_app.emit("auth-callback-received", ());
                    }
                }
            });
            #[cfg(any(target_os = "linux", all(debug_assertions, windows)))]
            app.deep_link().register_all()?;
            let window = app
                .get_webview_window("main")
                .expect("main panel window is configured");
            let token_path = app
                .path()
                .app_data_dir()
                .map_err(std::io::Error::other)?
                .join("ui-token");
            let websocket_token =
                load_or_create_websocket_token(&token_path).map_err(std::io::Error::other)?;
            app.manage(WebsocketToken(websocket_token.clone()));
            prepare_non_activating_window(&window)?;
            apply_panel_backdrop(&window);
            if let Some(key_window) = app.get_webview_window("keys") {
                let close_target = key_window.clone();
                key_window.on_window_event(move |event| {
                    if let tauri::WindowEvent::CloseRequested { api, .. } = event {
                        api.prevent_close();
                        let _ = close_target.hide();
                    }
                });
            }
            app.manage(PanelAnchor::default());
            app.manage(FocusBaseline::default());
            #[cfg(feature = "packaged-daemon")]
            let shortcut_status = {
                use tauri_plugin_global_shortcut::{Code, GlobalShortcutExt, Modifiers, Shortcut};

                let shortcut = Shortcut::new(Some(Modifiers::CONTROL), Code::Space);
                if app.global_shortcut().register(shortcut).is_ok() {
                    "available"
                } else {
                    "unavailable"
                }
            };
            #[cfg(not(feature = "packaged-daemon"))]
            let shortcut_status = if cfg!(feature = "focus-test") {
                "focus-test"
            } else {
                "development"
            };
            app.manage(ShortcutStatus(shortcut_status.to_string()));
            let focus_baseline = app.state::<FocusBaseline>().inner().clone();
            monitor_foreground(window.clone(), focus_baseline.clone());
            #[cfg(feature = "packaged-daemon")]
            {
                let supervisor = daemon::DaemonSupervisor::default();
                app.manage(supervisor.clone());
                daemon::supervise(app.handle().clone(), supervisor, websocket_token);
            }
            let tray_anchor = app.state::<PanelAnchor>().inner().clone();
            let recording_enabled = !cfg!(feature = "focus-test");

            let toggle = MenuItem::with_id(
                app,
                "toggle",
                "Start / stop recording · Ctrl+Space",
                recording_enabled,
                None::<&str>,
            )?;
            let cancel = MenuItem::with_id(
                app,
                "cancel",
                "Cancel recording",
                recording_enabled,
                None::<&str>,
            )?;
            let open = MenuItem::with_id(app, "open", "Show / hide Wicx", true, None::<&str>)?;
            let quit = MenuItem::with_id(app, "quit", "Quit Wicx", true, None::<&str>)?;
            let menu = Menu::with_items(app, &[&toggle, &cancel, &open, &quit])?;
            let tray = TrayIconBuilder::with_id("whisper")
                .icon(mic_tray_icon([132, 135, 145, 255]))
                .tooltip("Wicx")
                .menu(&menu)
                .show_menu_on_left_click(false)
                .on_tray_icon_event(move |_tray, event| {
                    if let TrayIconEvent::Click {
                        position,
                        button: MouseButton::Left,
                        button_state: MouseButtonState::Up,
                        ..
                    } = event
                    {
                        if window.is_visible().unwrap_or(false) {
                            let _ = window.hide();
                        } else {
                            let tray_x = position.x as i32;
                            let tray_y = position.y as i32;
                            if let Ok(mut anchor) = tray_anchor.0.lock() {
                                *anchor = (tray_x, tray_y);
                            }
                            show_panel(
                                &window,
                                &focus_baseline,
                                tray_x,
                                tray_y,
                                PANEL_HEIGHT as f64,
                            );
                            let _ = window.emit("panel-shown", ());
                        }
                    }
                })
                .on_menu_event(move |app, event| match event.id().as_ref() {
                    "toggle" => {
                        let _ = app.emit("recording-hotkey", ());
                    }
                    "cancel" => {
                        let _ = app.emit("cancel-recording", ());
                    }
                    "open" => toggle_panel(app),
                    "quit" => app.exit(0),
                    _ => {}
                })
                .build(app)?;
            app.manage(tray);
            Ok(())
        })
        .invoke_handler(tauri::generate_handler![
            resize_panel,
            set_tray_state,
            focus_test_build,
            recording_shortcut_status,
            websocket_token,
            get_auth_api_url,
            save_auth_api_url,
            start_oauth,
            complete_auth_callback,
            auth_is_signed_in,
            sign_out,
            fetch_billing_status,
            rewrite_transcript,
            fetch_billing_plans,
            create_checkout,
            manage_subscription,
            show_key_settings,
            open_diagnostics_folder
        ])
        .build(tauri::generate_context!())
        .expect("Wicx tray UI failed to start")
        .run(|app, event| {
            #[cfg(feature = "packaged-daemon")]
            if matches!(event, RunEvent::Exit) {
                app.state::<daemon::DaemonSupervisor>().request_shutdown();
            }
        })
}

#[cfg(test)]
mod tests {
    use super::{
        auth_handoff_code, load_or_create_websocket_token, panel_height_for_monitor,
        panel_position, public_billing_plans, public_billing_status, trusted_management_url,
        validated_api_origin,
    };
    use std::{fs, path::PathBuf};

    #[test]
    fn positions_panel_with_negative_monitor_origin_and_top_edge_clamping() {
        assert_eq!(
            panel_position((-1920, 0, 1920, 1080), (-10, 60), (570, 800), 8),
            (-578, 8)
        );
    }

    #[test]
    fn anchors_panel_above_tray_and_keeps_it_inside_right_monitor_edge() {
        assert_eq!(
            panel_position((0, 0, 1920, 1080), (1800, 900), (420, 560), 8),
            (1492, 332)
        );
    }

    #[test]
    fn expanded_panel_height_respects_scaled_monitor_bounds() {
        let height = panel_height_for_monitor(760.0, 1080, 1.5, 8);
        assert!((height - (1064.0 / 1.5)).abs() < 0.01);
    }

    #[test]
    fn expanded_panel_keeps_requested_height_when_it_fits() {
        assert_eq!(panel_height_for_monitor(760.0, 1440, 1.0, 8), 760.0);
    }

    #[test]
    fn websocket_token_is_reused_when_the_ui_shell_restarts() {
        let path = PathBuf::from(std::env::temp_dir())
            .join(format!("whisper-token-test-{}.txt", uuid::Uuid::new_v4()));
        let first = load_or_create_websocket_token(&path).expect("first token should be created");
        let second = load_or_create_websocket_token(&path).expect("token should be loaded again");
        assert_eq!(first, second);
        assert_eq!(uuid::Uuid::parse_str(&first).unwrap().to_string(), first);
        fs::remove_file(path).unwrap();
    }

    #[test]
    fn accepts_only_wicx_auth_callback_handoffs() {
        let code = "single-use-handoff-0123456789-abcdefghijklmnopqrstuvwxyz";
        assert_eq!(
            auth_handoff_code(&format!("wicx://auth/callback?code={code}")),
            Some(code.to_string())
        );
        assert_eq!(auth_handoff_code("wicx://auth/callback?code=short"), None);
        assert_eq!(
            auth_handoff_code(&format!("wicx://other/callback?code={code}")),
            None
        );
        assert_eq!(
            auth_handoff_code(&format!("wicx://auth/callback?code={code}&code={code}")),
            None
        );
        assert_eq!(
            auth_handoff_code(&format!("https://auth/callback?code={code}")),
            None
        );
    }

    #[test]
    fn validates_and_normalizes_service_origin() {
        assert_eq!(
            validated_api_origin("https://api.example.com/"),
            Ok("https://api.example.com".to_string())
        );
        assert!(validated_api_origin("https://api.example.com/v1").is_err());
        assert!(validated_api_origin("https://user:password@example.com").is_err());
        assert!(validated_api_origin("https://example.com/?token=secret").is_err());
        if cfg!(debug_assertions) {
            assert_eq!(
                validated_api_origin("http://localhost:8000"),
                Ok("http://localhost:8000".to_string())
            );
        } else {
            assert!(validated_api_origin("http://localhost:8000").is_err());
        }
    }

    #[test]
    fn exposes_only_safe_subscription_fields() {
        let status = public_billing_status(serde_json::json!({
            "user_id": "must-not-leak",
            "features": {"cloud_transcription": true, "diarization": false, "invalid": "yes"},
            "subscriptions": [{
                "plan_id": "pro-monthly",
                "status": "active",
                "current_period_end": "2026-11-01T00:00:00Z",
                "provider_subscription_id": "must-not-leak"
            }],
            "usage": {"cloud_transcription": {
                "plan_id": "pro-monthly", "period_end": "2026-11-01T00:00:00Z",
                "limit_milliseconds": 36000000, "used_milliseconds": 42000,
                "remaining_milliseconds": 35958000,
                "provider_subscription_id": "must-not-leak"
            }}
        }))
        .expect("valid billing response should be summarized");
        assert_eq!(status["features"]["cloud_transcription"], true);
        assert_eq!(status["features"]["diarization"], false);
        assert!(status.get("user_id").is_none());
        assert!(status["features"].get("invalid").is_none());
        assert!(status["subscriptions"][0]
            .get("provider_subscription_id")
            .is_none());
        assert_eq!(
            status["usage"]["cloud_transcription"]["used_milliseconds"],
            42000
        );
        assert!(status["usage"]["cloud_transcription"]
            .get("provider_subscription_id")
            .is_none());
    }

    #[test]
    fn filters_plan_catalog_and_never_exposes_provider_plan_codes() {
        let plans = public_billing_plans(serde_json::json!({
            "plans": [{
                "plan_id": "pro-monthly",
                "amount_minor": 2500,
                "currency": "GHS",
                "billing_mode": "recurring",
                "features": ["cloud_transcription"],
                "renewal": "automatic",
                "paystack_code": "PLN_secret"
            }, {
                "plan_id": "broken",
                "amount_minor": -10,
                "currency": "invalid",
                "billing_mode": "future"
            }]
        }))
        .expect("catalog response should be summarized");
        assert_eq!(plans["plans"].as_array().unwrap().len(), 1);
        assert_eq!(plans["plans"][0]["currency"], "GHS");
        assert!(plans["plans"][0].get("paystack_code").is_none());
    }

    #[test]
    fn accepts_only_paystack_subscription_management_links() {
        assert!(trusted_management_url(
            "https://paystack.com/manage/subscriptions/example?token=secret"
        ));
        assert!(!trusted_management_url(
            "https://checkout.paystack.com/manage/subscriptions/example?token=secret"
        ));
        assert!(!trusted_management_url(
            "https://paystack.com.evil.test/manage/subscriptions/example?token=secret"
        ));
        assert!(!trusted_management_url(
            "http://paystack.com/manage/subscriptions/example?token=secret"
        ));
        assert!(!trusted_management_url(
            "https://user@paystack.com/manage/subscriptions/example?token=secret"
        ));
    }
}
