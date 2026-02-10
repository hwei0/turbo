//! Logs QUIC recovery metrics (RTT, CWND) to Parquet files at configurable intervals.

use std::{
    collections::HashMap,
    path::PathBuf,
    time::{Duration, Instant},
};

use crate::{
    logging::utils::{create_flush_parquet, FileMetadata, VecEnum},
    utils::tokio_context::TokioContext,
};
use anyhow::Result;
use log::{debug, info};
pub struct BandwidthStatLog {
    file_metadata: FileMetadata,
    timestamp_list: Vec<f64>,
    instant_timestamp_list: Vec<f64>,
    rtt_list: Vec<f64>,
    cwnd_list: Vec<u32>,
    start_instant: Instant,
    tokio_context: TokioContext,
}

impl BandwidthStatLog {
    pub fn new(
        service_id: i32,
        file_dir: PathBuf,
        max_records: usize,
        start_instant: Instant,
        tokio_context: TokioContext,
    ) -> Self {
        Self {
            file_metadata: FileMetadata {
                file_idx: 0,
                file_dir,
                service_id,
                max_records,
            },
            timestamp_list: Vec::new(),
            instant_timestamp_list: Vec::new(),
            rtt_list: Vec::new(),
            cwnd_list: Vec::new(),
            start_instant,
            tokio_context,
        }
    }
    pub async fn append_record(&mut self, timestamp: f64, rtt: Duration, cwnd: u32) -> Result<()> {
        self.instant_timestamp_list
            .push(self.start_instant.elapsed().as_secs_f64());
        self.timestamp_list.push(timestamp);
        self.rtt_list.push(rtt.as_secs_f64());
        self.cwnd_list.push(cwnd);

        if self.timestamp_list.len() >= self.file_metadata.max_records {
            self.write_to_disk().await?;
        }

        Ok(())
    }

    pub async fn write_to_disk(&mut self) -> Result<()> {
        if self.timestamp_list.is_empty() {
            return Ok(());
        }
        info!(
            "bandwidth-stat-log write_to_disk START for service {}",
            self.file_metadata.service_id
        );
        let file_path = self.file_metadata.file_dir.join(format!(
            "bandwidth_stat_log_allservices_part{}.parquet",
            self.file_metadata.file_idx
        ));

        let mut column_map: HashMap<String, VecEnum> = HashMap::new();
        column_map.insert(
            "instant_timestamp".into(),
            VecEnum::VecF64(std::mem::take(&mut self.instant_timestamp_list)),
        );
        column_map.insert(
            "timestamp".into(),
            VecEnum::VecF64(std::mem::take(&mut self.timestamp_list)),
        );
        column_map.insert(
            "rtt".into(),
            VecEnum::VecF64(std::mem::take(&mut self.rtt_list)),
        );
        column_map.insert(
            "cwnd".into(),
            VecEnum::VecU32(std::mem::take(&mut self.cwnd_list)),
        );

        debug!(
            "bandwidth-stat-log write_to_disk acquiring lock for service {}",
            self.file_metadata.service_id
        );
        self.tokio_context
            .join_set
            .lock()
            .await
            .spawn_blocking(|| create_flush_parquet(file_path, column_map));
        info!(
            "bandwidth-stat-log write_to_disk DONE for service {}",
            self.file_metadata.service_id
        );
        self.file_metadata.file_idx += 1;

        Ok(())
    }
}
