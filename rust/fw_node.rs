//! Framework Node ABI v2 for Rust nodes.
//!
//! Vendored into each node repo as `src/fw_node.rs`, the same way a C header
//! would be. It knows nothing about any domain: the state type comes from
//! the schema the node implements. See ABI.md.
#![allow(dead_code)]

pub const ABI_VERSION: u32 = 2;

#[cfg(target_arch = "wasm32")]
#[panic_handler]
fn panic(_: &core::panic::PanicInfo) -> ! {
    core::arch::wasm32::unreachable()
}

/// Generates the ABI exports for a node.
///
/// - `$state`: the schema's state type (`#[repr(C)]`)
/// - `$zero`: a const all-zero value of it
/// - `$step`: `fn(&mut $state, &[f32], f32)`: state, params, dt
/// - `$defaults`: a `const [f32; N]` whose order matches `node.json` params
#[macro_export]
macro_rules! export_node {
    ($state:ty, $zero:expr, $step:path, $defaults:ident) => {
        static mut FW_STATE: $state = $zero;
        static mut FW_PARAMS: [f32; $defaults.len()] = $defaults;

        #[no_mangle]
        pub extern "C" fn fw_abi_version() -> u32 {
            $crate::fw_node::ABI_VERSION
        }

        #[no_mangle]
        pub extern "C" fn fw_state_ptr() -> u32 {
            (&raw const FW_STATE) as usize as u32
        }

        #[no_mangle]
        pub extern "C" fn fw_state_capacity() -> u32 {
            core::mem::size_of::<$state>() as u32
        }

        #[no_mangle]
        pub extern "C" fn fw_param_count() -> u32 {
            $defaults.len() as u32
        }

        #[no_mangle]
        pub extern "C" fn fw_set_param(index: u32, value: f32) -> u32 {
            let params = unsafe { &mut *(&raw mut FW_PARAMS) };
            match params.get_mut(index as usize) {
                Some(slot) => { *slot = value; 0 }
                None => 1,
            }
        }

        #[no_mangle]
        pub extern "C" fn fw_step(dt: f32) -> u32 {
            if !(dt > 0.0 && dt < 1.0) {
                return 2;
            }
            let state = unsafe { &mut *(&raw mut FW_STATE) };
            let params = unsafe { &*(&raw const FW_PARAMS) };
            $step(state, params, dt);
            0
        }
    };
}
