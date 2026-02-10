//! Shared logging utilities: Parquet file writing, type-erased column enums, and
//! file metadata tracking for numbered output files.

use anyhow::Result;
use polars::{
    frame::DataFrame,
    prelude::{Column, ParquetWriter},
};
use std::{collections::HashMap, path::PathBuf};
#[derive(Clone)]
pub(crate) struct FileMetadata {
    pub(crate) file_idx: i32,
    pub(crate) file_dir: PathBuf,
    pub(crate) service_id: i32,
    pub(crate) max_records: usize,
}

pub fn create_flush_parquet(
    file_path: PathBuf,
    mut column_map: HashMap<String, VecEnum>,
) -> Result<()> {
    let mut series_vec = Vec::new();

    for (colname, col) in column_map.iter_mut() {
        series_vec.push(col.to_column_mut(colname.clone()));
    }

    let mut file = std::fs::File::create(file_path)?;
    let mut df = DataFrame::new(series_vec)?;
    ParquetWriter::new(&mut file).finish(&mut df)?;

    Ok(())
}

pub struct RecordType {}

impl RecordType {
    pub fn image_overall() -> String {
        "IMAGE".to_string()
    }

    pub fn image_intermediate() -> String {
        "INTERMEDIATE".to_string()
    }

    pub fn enqueue_msg() -> String {
        "ENQUEUE_MSG".to_string()
    }

    pub fn shm_copy() -> String {
        "SHM_COPY".to_string()
    }

    pub fn flush() -> String {
        "FLUSH".to_string()
    }

    pub fn tx_rx() -> String {
        "TX_RX".to_string()
    }
}

pub enum VecEnum {
    // Type-erased column storage to support heterogeneous Parquet column types.
    VecI32(Vec<i32>),
    VecF64(Vec<f64>),
    VecU32(Vec<u32>),
    VecString(Vec<String>),
    VecI64(Vec<i64>),
}
impl VecEnum {
    fn to_column_mut(&mut self, col_name: String) -> Column {
        match self {
            VecEnum::VecI32(v) => Column::new(col_name.into(), v),
            VecEnum::VecF64(v) => Column::new(col_name.into(), v),
            VecEnum::VecU32(v) => Column::new(col_name.into(), v),
            VecEnum::VecString(v) => Column::new(col_name.into(), v),
            VecEnum::VecI64(v) => Column::new(col_name.into(), v),
        }
    }
}
