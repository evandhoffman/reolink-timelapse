from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # NVR connection
    nvr_host: str
    nvr_username: str
    nvr_password: str

    # Capture settings
    capture_interval: float = 15.0   # seconds between snapshots
    duration_hours: float = 18.0     # how long to capture

    # Stitch settings
    sample_rate: int = 1             # use every Nth captured frame (1 = all)
    output_fps: int = 24             # output video frame rate

    # Storage (must be a mounted volume in Docker)
    data_dir: str = "/data"
