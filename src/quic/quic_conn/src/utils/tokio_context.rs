//! Wrapper around tokio JoinSet for managing spawned async tasks.

use std::sync::Arc;
use tokio::{sync::Mutex, task::JoinSet};

#[derive(Clone)]
pub struct TokioContext {
    pub join_set: Arc<Mutex<JoinSet<Result<(), anyhow::Error>>>>,
}
impl TokioContext {
    pub fn new(join_set: Arc<Mutex<JoinSet<Result<(), anyhow::Error>>>>) -> Self {
        TokioContext { join_set }
    }
}
