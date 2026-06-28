from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    kafka_bootstrap: str = "kafka:9092"
    kafka_tasks_topic: str = "reconfig.tasks"
    kafka_results_topic: str = "results.reconfig"
    kafka_bootstrap_marker_enabled: bool = Field(
    default=True,
    alias="KAFKA_BOOTSTRAP_MARKER_ENABLED"
)

    victoriametrics_url: str = "http://victoriametrics:8428"
    metrics_poll_interval_sec: int = 60
    dedup_waiting_timeout_sec: int = 600
    vlan_resolution_method: str = "auto"
    netflow_src_encoding: str = "int"

    postgres_dsn: str = "postgresql+asyncpg://postgres:postgres@postgres/admin_panel"
    anomaly_threshold: float = 3.0
    bandwidth_threshold: float = 0.85
    trigger_change_percent: float = 0.2
    periodic_interval_seconds: int = 60
    segmenter_type: str = "greedy"
    sa_initial_temperature: float = 100.0
    sa_cooling_rate: float = 0.95
    sa_max_iterations: int = 1000
    greedy_max_iterations: int = 100
    lambda_coeff: float = 0.5
    quarantine_vlan: int = 999

    # Cold-start / low-variance guards for the z-score anomaly detector
    # (src/victoriametrics/client.py:_z_score). With a near-empty or constant
    # history a small fluctuation yields a huge z; both thresholds are
    # configurable so tuning does not require a code change.
    anomaly_min_window_size: int = Field(default=10, alias="ANOMALY_MIN_WINDOW_SIZE")
    anomaly_min_std_threshold: float = Field(default=1e-6, alias="ANOMALY_MIN_STD_THRESHOLD")
    # Lower bound on the window's mean below which a z-score from a single
    # small deviation would be disproportionately large (low-volume regime).
    # Guards the gap between cold-start (defect 1) and the low-std guard.
    anomaly_min_baseline_mean: float = Field(
        default=10.0, alias="ANOMALY_MIN_BASELINE_MEAN"
    )
    fallback_vlan_id: int = 1

    link_capacity_mbps: int = 1000
    icmp_threshold: float = 1000.0
    new_sources_threshold: int = 10
    inter_vlan_ratio_threshold: float = 0.4
    max_flow_bytes_threshold: float = 100_000_000
    # Absolute floor (bytes/sec) of cross-VLAN traffic required before a MERGE is
    # emitted. Guards against the auto-apply MERGE loop where a near-idle VLAN has
    # a noisy-but-high inter_vlan_ratio on negligible absolute traffic.
    merge_min_inter_vlan_bytes_per_sec: float = 1_000_000.0

    subinterface_parent: str = Field(default="GigabitEthernet0/0/1", alias="SUBINTERFACE_PARENT")
    subinterface_subnet_template: str = Field(
        default="192.168.{vlan_id}.1/24",
        alias="SUBINTERFACE_SUBNET_TEMPLATE",
    )
    subinterface_enabled: bool = Field(default=True, alias="SUBINTERFACE_ENABLED")

    # --- Initial topology sync gate -----------------------------------------
    # Before the first decision cycle, DE waits for CM's first successful
    # topology sync. The latest row in the dedicated topology_sync_status table
    # (written by CM) is the source of truth; the topology_sync_changed NOTIFY
    # only speeds up wakeups.
    wait_for_initial_sync: bool = Field(default=True, alias="WAIT_FOR_INITIAL_SYNC")
    # Upper bound on the startup wait. On expiry, behaviour is governed by
    # sync_wait_timeout_behavior.
    sync_wait_timeout_seconds: int = Field(default=120, alias="SYNC_WAIT_TIMEOUT_SECONDS")
    # Table poll cadence (race-free fallback when NOTIFY is missed/lost).
    sync_poll_interval_seconds: int = Field(default=5, alias="SYNC_POLL_INTERVAL_SECONDS")
    # Freshness guard against a stale OK. 0 disables the check (any OK accepted
    # at startup); >0 requires last_full_sync_at to be within this many seconds.
    sync_max_age_seconds: int = Field(default=0, alias="SYNC_MAX_AGE_SECONDS")
    # What to do when the wait times out: "proceed" (start with a warning) or
    # "keep_waiting" (never start until a usable sync appears).
    sync_wait_timeout_behavior: str = Field(default="proceed", alias="SYNC_WAIT_TIMEOUT_BEHAVIOR")

    log_level: str = "INFO"

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        populate_by_name=True,
    )

    @field_validator("quarantine_vlan")
    @classmethod
    def _validate_quarantine_vlan(cls, v: int) -> int:
        """Fail fast on a misconfigured quarantine VLAN: 0 or out-of-range
        values would otherwise propagate as silently-broken ISOLATE actions
        downstream."""
        if not 2 <= v <= 4094:
            raise ValueError(f"QUARANTINE_VLAN must be in [2, 4094], got {v}")
        return v


settings = Settings()
