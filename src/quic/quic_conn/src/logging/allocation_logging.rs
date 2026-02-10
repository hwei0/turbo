//! Logs bandwidth allocation changes (per-service allocation, model configs, expected
//! utility) to Parquet files.

use std::{collections::HashMap, path::PathBuf, time::Instant};

use crate::{
    logging::utils::{create_flush_parquet, FileMetadata, VecEnum},
    utils::tokio_context::TokioContext,
};
use anyhow::Result;
use log::{debug, info};

pub struct AllocationStatLog {
    file_metadata: FileMetadata,
    timestamp_list: Vec<f64>,
    instant_timestamp_list: Vec<f64>,
    service_id_list: Vec<i32>,
    allocation_list: HashMap<i32, Vec<f64>>,
    expected_utility_list: Vec<f64>,
    model_configuration_list: HashMap<i32, Vec<String>>,
    start_instant: Instant,
    tokio_context: TokioContext,
}

impl AllocationStatLog {
    pub fn new(
        service_id_list: Vec<i32>,
        file_dir: PathBuf,
        max_records: usize,
        start_instant: Instant,
        tokio_context: TokioContext,
    ) -> Self {
        let mut allocation_list = HashMap::new();
        let mut model_configuration_list = HashMap::new();

        for service in service_id_list.as_slice() {
            allocation_list.insert(*service, Vec::new());
            model_configuration_list.insert(*service, Vec::new());
        }
        Self {
            file_metadata: FileMetadata {
                file_idx: 0,
                file_dir,
                service_id: -1,
                max_records,
            },
            timestamp_list: Vec::new(),
            instant_timestamp_list: Vec::new(),
            service_id_list,
            allocation_list,
            expected_utility_list: Vec::new(),
            model_configuration_list,
            start_instant,
            tokio_context,
        }
    }

    pub async fn append_record(
        &mut self,
        timestamp: f64,
        allocations: HashMap<i32, f64>,
        expected_utility: f64,
        model_configs: HashMap<i32, String>,
    ) -> Result<()> {
        self.instant_timestamp_list
            .push(self.start_instant.elapsed().as_secs_f64());
        self.timestamp_list.push(timestamp);
        for service in self.service_id_list.iter() {
            self.allocation_list
                .get_mut(service)
                .expect("service must exist in allocation_list")
                .push(
                    *allocations
                        .get(service)
                        .expect("service must exist in allocations"),
                );
            self.model_configuration_list
                .get_mut(service)
                .expect("service must exist in model_configuration_list")
                .push(
                    model_configs
                        .get(service)
                        .expect("service must exist in model_configs")
                        .to_owned(),
                );
        }
        self.expected_utility_list.push(expected_utility);

        if self.timestamp_list.len() >= self.file_metadata.max_records {
            self.write_to_disk()
                .await
                .expect("AllocationStatLog write_to_disk must succeed");
        }

        Ok(())
    }

    pub async fn write_to_disk(&mut self) -> Result<()> {
        if self.timestamp_list.is_empty() {
            return Ok(());
        }

        info!(
            "allocation-stat-log write_to_disk START for service {}",
            self.file_metadata.service_id
        );

        let file_path = self.file_metadata.file_dir.join(format!(
            "allocation_stat_log_allservices_part{}.parquet",
            self.file_metadata.file_idx
        ));

        let mut column_map: HashMap<String, VecEnum> = HashMap::new();

        column_map.insert(
            "timestamp".into(),
            VecEnum::VecF64(std::mem::take(&mut self.timestamp_list)),
        );
        column_map.insert(
            "instant_timestamp".into(),
            VecEnum::VecF64(std::mem::take(&mut self.instant_timestamp_list)),
        );

        for service in self.service_id_list.as_slice() {
            column_map.insert(
                format!("allocation_service{service}"),
                VecEnum::VecF64(std::mem::take(
                    self.allocation_list
                        .get_mut(service)
                        .expect("service must exist in allocation_list"),
                )),
            );
            column_map.insert(
                format!("model_config_service{service}"),
                VecEnum::VecString(std::mem::take(
                    self.model_configuration_list
                        .get_mut(service)
                        .expect("service must exist in model_configuration_list"),
                )),
            );
        }
        column_map.insert(
            "expected_utility".into(),
            VecEnum::VecF64(std::mem::take(&mut self.expected_utility_list)),
        );

        debug!(
            "allocation-stat-log write_to_disk acquiring lock for service {}",
            self.file_metadata.service_id
        );

        self.tokio_context
            .join_set
            .lock()
            .await
            .spawn_blocking(|| create_flush_parquet(file_path, column_map));

        info!(
            "allocation-stat-log write_to_disk DONE for service {}",
            self.file_metadata.service_id
        );

        self.file_metadata.file_idx += 1;

        Ok(())
    }
}
