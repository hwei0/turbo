//! Transmission queue entry for the per-service send loop.
//!
//! Each TxQueueItem holds image byte data, context ID, transmission index, and a
//! timestamp for SLO timeout enforcement.

use std::time::Instant;

pub struct TxQueueItem {
    pub timestamp: Instant,
    pub image_context: i32,
    pub byte_data: Vec<u8>,
    pub tx_idx: usize,
}

impl TxQueueItem {
    pub fn new(byte_data: Vec<u8>, image_context: i32) -> Self {
        Self {
            timestamp: Instant::now(),
            image_context,
            byte_data,
            tx_idx: 0,
        }
    }
}
