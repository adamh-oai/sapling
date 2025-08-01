// @generated by autocargo

use std::env;
use std::fs;
use std::path::Path;
use thrift_compiler::Config;
use thrift_compiler::GenContext;
const CRATEMAP: &str = "\
fb303/thrift/fb303_core.thrift crate //fb303/thrift:fb303_core-rust
thrift/annotation/cpp.thrift cpp //thrift/annotation:cpp-rust
thrift/annotation/scope.thrift cpp->scope //thrift/annotation:scope-rust
thrift/annotation/thrift.thrift thrift //thrift/annotation:thrift-rust
";
#[rustfmt::skip]
fn main() {
    println!("cargo:rerun-if-changed=thrift_build.rs");
    let out_dir = env::var_os("OUT_DIR").expect("OUT_DIR env not provided");
    let cratemap_path = Path::new(&out_dir).join("cratemap");
    fs::write(cratemap_path, CRATEMAP).expect("Failed to write cratemap");
    Config::from_env(GenContext::Services)
        .expect("Failed to instantiate thrift_compiler::Config")
        .base_path("../../../..")
        .types_crate("fb303_core__types")
        .clients_crate("fb303_core__clients")
        .options("deprecated_default_enum_min_i32,serde")
        .run(["../../fb303_core.thrift"])
        .expect("Failed while running thrift compilation");
}
