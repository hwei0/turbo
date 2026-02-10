//! Shared library crate for the QUIC transport layer (quic_conn).
//!
//! Provides core infrastructure used by both quic_client and quic_server:
//!   - `core_routines`: async task loops for sending, receiving, and bandwidth polling
//!   - `managers`: WeightedStreamManager and BandwidthManager for per-service orchestration
//!   - `logging`: Parquet-based logging for bandwidth, allocation, network, and image context
//!   - `shmem`: POSIX shared memory helpers for zero-copy IPC with Python processes
//!   - `utils`: YAML config parsing, QUIC recovery metric subscribers, and tokio helpers

pub mod core_routines;
pub mod logging;
pub mod managers;
pub mod shmem;
pub mod utils;
