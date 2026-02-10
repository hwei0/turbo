//! Per-service TX/RX byte count logging to Parquet files.

use std::{collections::HashMap, path::PathBuf, time::Instant};

use crate::{
    logging::utils::{create_flush_parquet, FileMetadata, VecEnum},
    utils::tokio_context::TokioContext,
};
use anyhow::Result;
use log::{debug, info};

pub struct NetworkStatLogConfig {
    pub network_stat_log_file_dir: PathBuf,
    pub network_stat_log_capacity: usize,
    pub enable_network_stat_log: bool,
}

pub struct NetworkStatLog {
    file_metadata: FileMetadata,
    instant_timestamp_list: Vec<f64>,
    timestamp_list: Vec<f64>,
    tx_cnt_list: Vec<i64>,
    rx_cnt_list: Vec<i64>,
    start_instant: Instant,
    tokio_context: TokioContext,
}

impl NetworkStatLog {
    pub fn new(
        service_id: i32,
        file_dir: PathBuf,
        max_records: usize,
        start_instant: Instant,
        tokio_context: TokioContext,
    ) -> Self {
        NetworkStatLog {
            file_metadata: FileMetadata {
                file_idx: 0,
                file_dir,
                service_id,
                max_records,
            },
            timestamp_list: Vec::new(),
            instant_timestamp_list: Vec::new(),
            tx_cnt_list: Vec::new(),
            rx_cnt_list: Vec::new(),
            start_instant,
            tokio_context,
        }
    }
    pub async fn append_record(&mut self, timestamp: f64, tx_cnt: i64, rx_cnt: i64) -> Result<()> {
        self.instant_timestamp_list
            .push(self.start_instant.elapsed().as_secs_f64());
        self.timestamp_list.push(timestamp);
        self.tx_cnt_list.push(tx_cnt);
        self.rx_cnt_list.push(rx_cnt);

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
            "network-stat-log write_to_disk START for service {}",
            self.file_metadata.service_id
        );

        let file_path = self.file_metadata.file_dir.join(format!(
            "network_stat_log_service{}_part{}.parquet",
            self.file_metadata.service_id, self.file_metadata.file_idx
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
            "tx_cnt".into(),
            VecEnum::VecI64(std::mem::take(&mut self.tx_cnt_list)),
        );
        column_map.insert(
            "rx_cnt".into(),
            VecEnum::VecI64(std::mem::take(&mut self.rx_cnt_list)),
        );

        debug!(
            "network-stat-log write_to_disk acquiring lock for service {}",
            self.file_metadata.service_id
        );

        self.tokio_context
            .join_set
            .lock()
            .await
            .spawn_blocking(|| create_flush_parquet(file_path, column_map));

        info!(
            "network-stat-log write_to_disk DONE for service {}",
            self.file_metadata.service_id
        );

        self.file_metadata.file_idx += 1;

        Ok(())
    }
}
