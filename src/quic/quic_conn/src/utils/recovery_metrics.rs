//! s2n-quic event subscriber for capturing QUIC recovery metrics (RTT, CWND).
//!
//! CustomRecoverySubscriber implements the s2n-quic Subscriber trait and atomically
//! updates a RecoverySnapshot with the latest RTT (seconds) and CWND (bytes) from
//! QUIC recovery events. These values are read by the bandwidth_refresh_loop to
//! report network conditions to the BandwidthAllocator.

use std::sync::{atomic::AtomicU32, Arc};

use atomic_float::AtomicF64;
use log::trace;
use s2n_quic::provider::event::{self, events::RecoveryMetrics, ConnectionMeta, Subscriber};
use std::sync::atomic::Ordering::SeqCst;
pub struct RecoverySnapshot {
    pub rtt: AtomicF64, //in secs
    pub cwnd: AtomicU32,
    pub timestamp: AtomicF64, // in secs
}
pub struct CustomRecoverySubscriber {
    pub recovery_ptr: Arc<RecoverySnapshot>,
}

impl Subscriber for CustomRecoverySubscriber {
    type ConnectionContext = ();

    /// Initialize the Connection Context.
    fn create_connection_context(
        &mut self,
        _meta: &ConnectionMeta,
        _info: &event::ConnectionInfo,
    ) -> Self::ConnectionContext {
    }

    fn on_recovery_metrics(
        &mut self,
        _context: &mut Self::ConnectionContext,
        meta: &ConnectionMeta,
        event: &RecoveryMetrics,
    ) {
        trace!("on_recovery_metrics: time={}, rtt={}, cwnd={}", meta.timestamp.duration_since_start().as_secs_f64(), event.smoothed_rtt.as_secs_f32(), event.congestion_window);
        self.recovery_ptr
            .timestamp
            .store(meta.timestamp.duration_since_start().as_secs_f64(), SeqCst);
        self.recovery_ptr
            .rtt
            .store(event.smoothed_rtt.as_secs_f64(), SeqCst);
        self.recovery_ptr
            .cwnd
            .store(event.congestion_window, SeqCst); //we switch to atomics, because blocking or awaiting on mutex is not possible in this synchronous context that also hosts await calls
    }
}
