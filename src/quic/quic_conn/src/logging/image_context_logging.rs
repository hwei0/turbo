//! Detailed per-image transmission tracing logged to Parquet files.
//!
//! Tracks begin/end times for each stage of image processing: SHM_COPY, ENQUEUE_MSG,
//! FLUSH, TX_RX operations. Logs both unix epoch and experiment-relative timestamps,
//! image size, context ID, and ACK delays. Separate incoming/outgoing logs per service.

use std::{
    collections::HashMap,
    path::PathBuf,
    time::{Duration, Instant, SystemTime, UNIX_EPOCH},
};

use crate::{
    logging::utils::{create_flush_parquet, FileMetadata, VecEnum},
    utils::tokio_context::TokioContext,
};
use anyhow::Result;
use log::{debug, info};

pub struct ImageContextLogConfig {
    pub image_context_log_file_dir: PathBuf,
    pub image_context_log_capacity: usize,
    pub enable_image_context_log_outgoing: bool,
    pub enable_image_context_log_incoming: bool,
}

pub struct ImageContextLog {
    is_outgoing_to_quic: bool,
    file_metadata: FileMetadata,
    image_context_list: Vec<i32>,
    image_begin_time_secs: Vec<f64>,
    image_end_time_secs: Vec<f64>,
    image_end_time_secs_normalized: Vec<f64>,
    image_begin_time_unix_epoch_secs: Vec<f64>,
    image_end_time_unix_epoch_secs: Vec<f64>,
    record_type: Vec<String>,
    ack_delay_secs: Vec<f64>,
    image_size: Vec<i32>,
    tokio_context: TokioContext,
    start_instant: Instant,
}

impl ImageContextLog {
    pub fn new(
        service_id: i32,
        file_dir: PathBuf,
        max_records: usize,
        is_outgoing_to_quic: bool,
        start_instant: Instant,
        tokio_context: TokioContext,
    ) -> Self {
        ImageContextLog {
            file_metadata: FileMetadata {
                file_idx: 0,
                file_dir,
                service_id,
                max_records,
            },
            image_context_list: Vec::new(),
            image_begin_time_secs: Vec::new(),
            image_end_time_secs: Vec::new(),
            image_end_time_secs_normalized: Vec::new(),
            image_begin_time_unix_epoch_secs: Vec::new(),
            image_end_time_unix_epoch_secs: Vec::new(),
            tokio_context,
            is_outgoing_to_quic,
            start_instant,
            ack_delay_secs: Vec::new(),
            image_size: Vec::new(),
            record_type: Vec::new(),
        }
    }

    pub async fn append_begin_record(
        &mut self,
        image_context: i32,
        image_size: i32,
        record_type: String,
    ) -> Result<()> {
        self.image_context_list.push(image_context);
        self.image_begin_time_unix_epoch_secs.push(
            SystemTime::now()
                .duration_since(UNIX_EPOCH)
                .expect("system clock must not be before UNIX_EPOCH")
                .as_secs_f64(),
        );
        self.image_begin_time_secs
            .push(self.start_instant.elapsed().as_secs_f64());
        self.image_end_time_secs.push(f64::NAN);
        self.image_end_time_secs_normalized.push(f64::NAN);
        self.image_end_time_unix_epoch_secs.push(f64::NAN);
        self.ack_delay_secs.push(f64::NAN);
        self.image_size.push(image_size);
        self.record_type.push(record_type);

        Ok(())
    }

    pub async fn append_end_record(
        &mut self,
        image_context: i32,
        ack_delay: Duration,
        record_type: String,
    ) -> Result<()> {
        self.image_context_list.push(image_context);
        self.image_begin_time_unix_epoch_secs.push(f64::NAN);
        self.image_begin_time_secs.push(f64::NAN);
        let end_time = self.start_instant.elapsed().as_secs_f64();
        self.image_end_time_secs.push(end_time);
        self.image_end_time_secs_normalized
            .push(end_time - ack_delay.as_secs_f64());
        self.image_end_time_unix_epoch_secs.push(
            SystemTime::now()
                .duration_since(UNIX_EPOCH)
                .expect("system clock must not be before UNIX_EPOCH")
                .as_secs_f64(),
        );
        self.ack_delay_secs.push(ack_delay.as_secs_f64());
        self.image_size.push(-1);
        self.record_type.push(record_type);

        if self.image_context_list.len() >= self.file_metadata.max_records {
            self.write_to_disk().await?;
        }

        Ok(())
    }

    pub async fn write_to_disk(&mut self) -> Result<()> {
        if self.image_context_list.is_empty() {
            return Ok(());
        }
        info!(
            "image-context-log write_to_disk START for service {}",
            self.file_metadata.service_id
        );

        let file_path = self.file_metadata.file_dir.join(format!(
            "image_context_log_{}_service{}_part{}",
            if self.is_outgoing_to_quic {
                "outgoing"
            } else {
                "incoming"
            },
            self.file_metadata.service_id,
            self.file_metadata.file_idx
        ));

        let mut column_map: HashMap<String, VecEnum> = HashMap::new();
        column_map.insert(
            "image_context".into(),
            VecEnum::VecI32(std::mem::take(&mut self.image_context_list)),
        );
        column_map.insert(
            "begin_time_secs".into(),
            VecEnum::VecF64(std::mem::take(&mut self.image_begin_time_secs)),
        );
        column_map.insert(
            "end_time_secs".into(),
            VecEnum::VecF64(std::mem::take(&mut self.image_end_time_secs)),
        );
        column_map.insert(
            "end_time_secs_normalized".into(),
            VecEnum::VecF64(std::mem::take(&mut self.image_end_time_secs_normalized)),
        );
        column_map.insert(
            "begin_time_epoch_secs".into(),
            VecEnum::VecF64(std::mem::take(&mut self.image_begin_time_unix_epoch_secs)),
        );
        column_map.insert(
            "end_time_epoch_secs".into(),
            VecEnum::VecF64(std::mem::take(&mut self.image_end_time_unix_epoch_secs)),
        );
        column_map.insert(
            "ack_delay_secs".into(),
            VecEnum::VecF64(std::mem::take(&mut self.ack_delay_secs)),
        );
        column_map.insert(
            "image_size_bytes".into(),
            VecEnum::VecI32(std::mem::take(&mut self.image_size)),
        );
        column_map.insert(
            "record_type".into(),
            VecEnum::VecString(std::mem::take(&mut self.record_type)),
        );

        debug!(
            "image-context-log write_to_disk acquiring lock for service {}",
            self.file_metadata.service_id
        );

        self.tokio_context
            .join_set
            .lock()
            .await
            .spawn_blocking(|| create_flush_parquet(file_path, column_map));

        info!(
            "image-context-log write_to_disk DONE for service {}",
            self.file_metadata.service_id
        );

        self.file_metadata.file_idx += 1;

        Ok(())
    }
}
