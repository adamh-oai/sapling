/*
 * Copyright (c) Meta Platforms, Inc. and affiliates.
 *
 * This software may be used and distributed according to the terms of the
 * GNU General Public License version 2.
 */

use anyhow::Result;
use mononoke_types::RepositoryId;
use serde_derive::Deserialize;
use serde_derive::Serialize;
use sql::mysql;
use sql::mysql_async::FromValueError;
use sql::mysql_async::Value;
use sql::mysql_async::from_value_opt;
use sql::mysql_async::prelude::ConvIr;
use sql::mysql_async::prelude::FromValue;

#[derive(Clone, Copy, Debug, Eq, PartialEq, Serialize, Deserialize)]
#[derive(mysql::OptTryFromRowField)]
#[derive(bincode::Encode, bincode::Decode)]
pub struct RowId(pub u64);

impl From<RowId> for Value {
    fn from(id: RowId) -> Self {
        Value::UInt(id.0)
    }
}

impl std::fmt::Display for RowId {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        write!(f, "{}", self.0)
    }
}

impl ConvIr<RowId> for RowId {
    fn new(v: Value) -> Result<Self, FromValueError> {
        Ok(RowId(from_value_opt(v)?))
    }
    fn commit(self) -> Self {
        self
    }
    fn rollback(self) -> Value {
        self.into()
    }
}

impl FromValue for RowId {
    type Intermediate = RowId;
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct PushRedirectionConfigEntry {
    pub id: RowId,
    pub repo_id: RepositoryId,
    pub draft_push: bool,
    pub public_push: bool,
}
