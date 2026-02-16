//! YAML configuration parser for QUIC client/server settings.
//!
//! Parses timing parameters, initial bandwidth allocation, ZMQ socket paths, service
//! IDs, logging capacities, and junk service settings from the quic_config_*.yaml files.

use std::time::Duration;

use crate::utils::TimingConfig;

use config::Config;
pub struct QuicConfig {
    pub timing_config: TimingConfig,
    pub init_allocation: f64,
    pub zmq_dir: String,
    pub services: Vec<i32>,
}

impl QuicConfig {
    pub fn read_from_config(config: Config) -> QuicConfig {
        let timing_config: TimingConfig = TimingConfig {
            slo_timeout: Duration::from_millis(
                config
                    .get_int("slo_timeout_ms")
                    .expect("config must contain 'slo_timeout_ms'")
                    .try_into()
                    .expect("slo_timeout_ms must fit in u64"),
            ),
            junk_tx_loop_interval: Duration::from_millis(
                config
                    .get_int("junk_tx_loop_interval_ms")
                    .expect("config must contain 'junk_tx_loop_interval_ms'")
                    .try_into()
                    .expect("junk_tx_loop_interval_ms must fit in u64"),
            ),
            logging_interval: Duration::from_millis(
                config
                    .get_int("logging_interval_ms")
                    .expect("config must contain 'logging_interval_ms'")
                    .try_into()
                    .expect("logging_interval_ms must fit in u64"),
            ),
            bw_polling_interval: Duration::from_millis(
                config
                    .get_int("bw_polling_interval_ms")
                    .expect("config must contain 'bw_polling_interval_ms'")
                    .try_into()
                    .expect("bw_polling_interval_ms must fit in u64"),
            ),
            bw_update_interval: Duration::from_millis(
                config
                    .get_int("bw_update_interval_ms")
                    .expect("config must contain 'bw_update_interval_ms'")
                    .try_into()
                    .expect("bw_update_interval_ms must fit in u64"),
            ),
            max_junk_payload_megab: config
                .get_float("max_junk_payload_Mb")
                .expect("config must contain 'max_junk_payload_Mb'"),
            junk_restart_interval: Duration::from_millis(
                config
                    .get_int("junk_restart_interval_ms")
                    .expect("config must contain 'junk_restart_interval_ms'")
                    as u64,
            ),
        };
        let init_allocation = config
            .get_float("init_allocation")
            .expect("config must contain 'init_allocation'");
        let zmq_dir = config
            .get_string("zmq_dir")
            .expect("config must contain 'zmq_dir'");
        let services: Vec<config::Value> = config
            .get_array("services")
            .expect("config must contain 'services' array");

        QuicConfig {
            timing_config,
            init_allocation,
            zmq_dir,
            services: Vec::from_iter(services.iter().map(|v| {
                v.clone()
                    .into_int()
                    .expect("service value must be an integer")
                    .try_into()
                    .expect("service id must fit in i32")
            })),
        }
    }
}
