/**
 * Copyright (c) Meta Platforms, Inc. and affiliates.
 *
 * This source code is licensed under the MIT license found in the
 * LICENSE file in the root directory of this source tree.
 */

import type {Author, DateTuple, Hash, RepoPath} from './common';

/* Types for the `debugimportstack` and `debugoutputstack` commands. */

/**
 * Placeholder commit hash to identify "to be created" commits.
 * Starts with ":". Can be referred by "parents" of other commits.
 */
export type Mark = string;

/** Matches output of debugexportstack. See debugstack.py. */
export type ExportStack = ExportCommit[];

export type ExportCommit = {
  /** `true` for commits explicitly requested via debugstack command. */
  requested: boolean;
  /** Commit hash. `ffffffffffffffffffffffffffffffffffffffff` (`node.wdirhex`) means the working copy. */
  node: Hash;
  author: Author;
  date: DateTuple;
  /* Commit message. */
  text: string;
  /** `true` for public commits. */
  immutable: boolean;
  parents?: Hash[];
  /** Files changed by this commit. `null` means the file is deleted. */
  files?: Map<RepoPath, ExportFile | null>;
  relevantFiles?: Map<RepoPath, ExportFile | null>;
};

export type ExportFile = {
  /** UTF-8 content. */
  data?: string;
  /** Binary content encoded in base85. */
  dataBase85?: string;
  /** If present, this file is copied (or renamed) from another file. */
  copyFrom?: RepoPath;
  /** 'x': executable. 'l': symlink. 'm': submodule. */
  flags?: string;
};

/** Matches input of debugimportstack. See debugstack.py. */
export type ImportStack = ImportAction[];

export type ImportAction = ['commit', ImportCommit] | ['goto', ImportGoto] | ['reset', ImportReset];

export type ImportCommit = {
  /** Placeholder commit hash. Must start with ":". */
  mark: Mark;
  author: Author;
  date: DateTuple;
  /** Commit message. */
  text: string;
  parents: (Hash | Mark)[];
  predecessors?: (Hash | Mark)[];
  /** Why predecessors are obsoleted? For example, 'amend', 'split', 'histedit'. */
  operation?: string;
  files: Map<RepoPath, ExportFile | null>;
};

/** Update the "current commit" without changing the working copy. */
export type ImportReset = {mark: Mark};

/** Checkout the given commit. */
export type ImportGoto = {mark: Mark};

/** Matches output of debugimportstack. See debugstack.py. */
export type ImportedStack = ImportedCommit[];

/** The given `mark` has a known commit hash `node`. */
export type ImportedCommit = {node: Hash; mark: Mark};