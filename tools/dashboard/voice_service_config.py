"""Process-start voice deployment configuration."""
import os


def enabled() -> bool:
    value = os.environ.get("VOICE_SIDECAR_ENABLED", "false").lower()
    if value not in {"true", "false"}:
        raise ValueError("VOICE_SIDECAR_ENABLED must be true or false")
    return value == "true"


def public_port() -> int:
    port = int(os.environ.get("VOICE_PUBLIC_PORT", "8443"))
    if not 1 <= port <= 65535:
        raise ValueError("VOICE_PUBLIC_PORT must be a valid port")
    return port


def meta() -> str:
    return f'<meta name="autonomy-voice-port" content="{public_port()}">' if enabled() else ""


def validate_compose_mode() -> None:
    if os.environ.get("VOICE_COMPOSE_MODE") != "true":
        return
    profiles = {item.strip() for item in os.environ.get("COMPOSE_PROFILES", "").split(",")}
    if ("voice" in profiles) != enabled():
        raise ValueError("Set COMPOSE_PROFILES=voice and VOICE_SIDECAR_ENABLED=true together, or disable both")
    public_port()


if __name__ == "__main__":
    validate_compose_mode()
