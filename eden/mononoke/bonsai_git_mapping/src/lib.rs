/*
 * Copyright (c) Meta Platforms, Inc. and affiliates.
 *
 * This software may be used and distributed according to the terms of the
 * GNU General Public License version 2.
 */

use std::collections::HashMap;
use std::collections::HashSet;

use ::sql_ext::Transaction;
use anyhow::Result;
use anyhow::ensure;
use ascii::AsciiStr;
use async_trait::async_trait;
use context::CoreContext;
use mononoke_types::BonsaiChangeset;
use mononoke_types::ChangesetId;
use mononoke_types::RepositoryId;
use mononoke_types::hash::GitSha1;
use slog::warn;

mod caching;
mod errors;
mod nodehash;
mod sql;

pub use crate::caching::CachingBonsaiGitMapping;
pub use crate::errors::AddGitMappingErrorKind;
pub use crate::nodehash::GitSha1Prefix;
pub use crate::nodehash::GitSha1sResolvedFromPrefix;
pub use crate::sql::SqlBonsaiGitMapping;
pub use crate::sql::SqlBonsaiGitMappingBuilder;

#[derive(Clone, Debug, Eq, Hash, PartialEq)]
pub struct BonsaiGitMappingEntry {
    pub git_sha1: GitSha1,
    pub bcs_id: ChangesetId,
}

impl BonsaiGitMappingEntry {
    pub fn new(git_sha1: GitSha1, bcs_id: ChangesetId) -> Self {
        BonsaiGitMappingEntry { git_sha1, bcs_id }
    }
}

#[derive(Debug, Clone, Eq, PartialEq, Hash)]
pub enum BonsaisOrGitShas {
    Bonsai(Vec<ChangesetId>),
    GitSha1(Vec<GitSha1>),
}

impl BonsaisOrGitShas {
    pub fn from_object_ids(oids: impl Iterator<Item = impl AsRef<gix_hash::oid>>) -> Result<Self> {
        let shas = oids
            .map(|oid| GitSha1::from_object_id(oid.as_ref()))
            .collect::<Result<Vec<_>>>()?;
        Ok(BonsaisOrGitShas::GitSha1(shas))
    }

    pub fn is_empty(&self) -> bool {
        match self {
            BonsaisOrGitShas::Bonsai(v) => v.is_empty(),
            BonsaisOrGitShas::GitSha1(v) => v.is_empty(),
        }
    }

    pub fn count(&self) -> usize {
        match self {
            BonsaisOrGitShas::Bonsai(v) => v.len(),
            BonsaisOrGitShas::GitSha1(v) => v.len(),
        }
    }
}

impl From<ChangesetId> for BonsaisOrGitShas {
    fn from(cs_id: ChangesetId) -> Self {
        BonsaisOrGitShas::Bonsai(vec![cs_id])
    }
}

impl From<Vec<ChangesetId>> for BonsaisOrGitShas {
    fn from(cs_ids: Vec<ChangesetId>) -> Self {
        BonsaisOrGitShas::Bonsai(cs_ids)
    }
}

impl From<GitSha1> for BonsaisOrGitShas {
    fn from(git_sha1: GitSha1) -> Self {
        BonsaisOrGitShas::GitSha1(vec![git_sha1])
    }
}

impl From<Vec<GitSha1>> for BonsaisOrGitShas {
    fn from(revs: Vec<GitSha1>) -> Self {
        BonsaisOrGitShas::GitSha1(revs)
    }
}

#[facet::facet]
#[async_trait]
pub trait BonsaiGitMapping: Send + Sync {
    fn repo_id(&self) -> RepositoryId;

    async fn add(
        &self,
        ctx: &CoreContext,
        entry: BonsaiGitMappingEntry,
    ) -> Result<(), AddGitMappingErrorKind> {
        self.bulk_add(ctx, &[entry]).await
    }

    async fn bulk_add(
        &self,
        ctx: &CoreContext,
        entries: &[BonsaiGitMappingEntry],
    ) -> Result<(), AddGitMappingErrorKind>;

    async fn bulk_add_git_mapping_in_transaction(
        &self,
        ctx: &CoreContext,
        entries: &[BonsaiGitMappingEntry],
        transaction: Transaction,
    ) -> Result<Transaction, AddGitMappingErrorKind>;

    async fn get(
        &self,
        ctx: &CoreContext,
        field: BonsaisOrGitShas,
    ) -> Result<Vec<BonsaiGitMappingEntry>>;

    async fn get_git_sha1_from_bonsai(
        &self,
        ctx: &CoreContext,
        bcs_id: ChangesetId,
    ) -> Result<Option<GitSha1>> {
        let result = self
            .get(ctx, BonsaisOrGitShas::Bonsai(vec![bcs_id]))
            .await?;
        Ok(result.into_iter().next().map(|entry| entry.git_sha1))
    }

    async fn get_bonsai_from_git_sha1(
        &self,
        ctx: &CoreContext,
        git_sha1: GitSha1,
    ) -> Result<Option<ChangesetId>> {
        let result = self
            .get(ctx, BonsaisOrGitShas::GitSha1(vec![git_sha1]))
            .await?;
        Ok(result.into_iter().next().map(|entry| entry.bcs_id))
    }

    async fn get_many_git_sha1_by_prefix(
        &self,
        ctx: &CoreContext,
        cs_prefix: GitSha1Prefix,
        limit: usize,
    ) -> Result<GitSha1sResolvedFromPrefix> {
        let mut fetched_cs = self
            .get_in_range(ctx, cs_prefix.min_cs(), cs_prefix.max_cs(), limit + 1)
            .await?;
        let res = match fetched_cs.len() {
            0 => GitSha1sResolvedFromPrefix::NoMatch,
            1 => GitSha1sResolvedFromPrefix::Single(fetched_cs[0].clone()),
            l if l <= limit => GitSha1sResolvedFromPrefix::Multiple(fetched_cs),
            _ => GitSha1sResolvedFromPrefix::TooMany({
                fetched_cs.pop();
                fetched_cs
            }),
        };
        Ok(res)
    }

    async fn bulk_import_from_bonsai(
        &self,
        ctx: &CoreContext,
        changesets: &[BonsaiChangeset],
    ) -> Result<()> {
        let mut entries = vec![];
        for bcs in changesets.iter() {
            match extract_git_sha1_from_bonsai_extra(bcs.hg_extra()) {
                Ok(Some(git_sha1)) => {
                    let entry = BonsaiGitMappingEntry::new(git_sha1, bcs.get_changeset_id());
                    entries.push(entry);
                }
                Ok(None) => {
                    warn!(
                        ctx.logger(),
                        "The git mapping is missing in bonsai commit extras: {:?}",
                        bcs.get_changeset_id()
                    );
                }
                Err(e) => {
                    warn!(ctx.logger(), "Couldn't fetch git mapping: {:?}", e);
                }
            }
        }
        self.bulk_add(ctx, &entries).await?;
        Ok(())
    }

    async fn get_in_range(
        &self,
        ctx: &CoreContext,
        low: GitSha1,
        high: GitSha1,
        limit: usize,
    ) -> Result<Vec<GitSha1>>;

    /// Convert a set of git commit ids to bonsai changesets.  If a changeset doesn't exist, it is omitted from the result.
    async fn convert_available_git_to_bonsai(
        &self,
        ctx: &CoreContext,
        git_sha1s: Vec<GitSha1>,
    ) -> Result<Vec<ChangesetId>> {
        let mapping = self.get(ctx, git_sha1s.into()).await?;
        Ok(mapping.into_iter().map(|entry| entry.bcs_id).collect())
    }

    /// Convert a set of git commit ids to bonsai changesets.  If a changeset doesn't exist, this is an error.
    async fn convert_all_git_to_bonsai(
        &self,
        ctx: &CoreContext,
        git_sha1s: Vec<GitSha1>,
    ) -> Result<Vec<ChangesetId>> {
        let mapping = self.get(ctx, git_sha1s.clone().into()).await?;
        if mapping.len() != git_sha1s.len() {
            let mut result = Vec::with_capacity(mapping.len());
            let mut missing = git_sha1s.into_iter().collect::<HashSet<_>>();
            for entry in mapping {
                missing.remove(&entry.git_sha1);
                result.push(entry.bcs_id);
            }
            ensure!(
                missing.is_empty(),
                "Missing bonsai mapping for git commits: {:?}",
                missing,
            );
            Ok(result)
        } else {
            Ok(mapping.into_iter().map(|entry| entry.bcs_id).collect())
        }
    }

    /// Convert a set of bonsai changeset ids to git commits.  If a changeset doesn't exist, it is omitted from the result.
    async fn convert_available_bonsai_to_git(
        &self,
        ctx: &CoreContext,
        bcs_ids: Vec<ChangesetId>,
    ) -> Result<Vec<GitSha1>> {
        let mapping = self.get(ctx, bcs_ids.into()).await?;
        Ok(mapping.into_iter().map(|entry| entry.git_sha1).collect())
    }

    /// Convert a set of bonsai changeset ids to git commits.  If a changeset doesn't exist, this is an error.
    async fn convert_all_bonsai_to_hg(
        &self,
        ctx: &CoreContext,
        bcs_ids: Vec<ChangesetId>,
    ) -> Result<Vec<GitSha1>> {
        let mapping = self.get(ctx, bcs_ids.clone().into()).await?;
        if mapping.len() != bcs_ids.len() {
            let mut result = Vec::with_capacity(mapping.len());
            let mut missing = bcs_ids.into_iter().collect::<HashSet<_>>();
            for entry in mapping {
                missing.remove(&entry.bcs_id);
                result.push(entry.git_sha1);
            }
            ensure!(
                missing.is_empty(),
                "Missing git mapping for bonsai changesets: {:?}",
                missing,
            );
            Ok(result)
        } else {
            Ok(mapping.into_iter().map(|entry| entry.git_sha1).collect())
        }
    }

    /// Get a hashmap that maps from given bonsai changesets to their hg equivalent.
    async fn get_bonsai_to_git_map(
        &self,
        ctx: &CoreContext,
        bcs_ids: Vec<ChangesetId>,
    ) -> Result<HashMap<ChangesetId, GitSha1>> {
        let mapping = self.get(ctx, bcs_ids.into()).await?;
        Ok(mapping
            .into_iter()
            .map(|entry| (entry.bcs_id, entry.git_sha1))
            .collect())
    }
}

pub const HGGIT_SOURCE_EXTRA: &str = "hg-git-rename-source";
pub const CONVERT_REVISION_EXTRA: &str = "convert_revision";

pub fn extract_git_sha1_from_bonsai_extra<'a, 'b, T>(extra: T) -> Result<Option<GitSha1>>
where
    T: Iterator<Item = (&'a str, &'b [u8])>,
{
    let (mut hggit_source_extra, mut convert_revision_extra) = (None, None);
    for (key, value) in extra {
        if key == HGGIT_SOURCE_EXTRA {
            hggit_source_extra = Some(value);
        }
        if key == CONVERT_REVISION_EXTRA {
            convert_revision_extra = Some(value);
        }
    }

    if hggit_source_extra == Some("git".as_bytes()) {
        if let Some(convert_revision_extra) = convert_revision_extra {
            let git_sha1 = AsciiStr::from_ascii(convert_revision_extra)?;
            let git_sha1 = GitSha1::from_ascii_str(git_sha1)?;
            return Ok(Some(git_sha1));
        }
    }
    Ok(None)
}
