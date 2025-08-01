# Copyright (c) Meta Platforms, Inc. and affiliates.
#
# This software may be used and distributed according to the terms of the
# GNU General Public License version 2.

"""apply working directory changes to changesets

The absorb extension provides a command to use annotate information to
amend modified chunks into the corresponding non-public changesets.

::

    [absorb]
    # only check 50 recent non-public changesets at most
    maxstacksize = 50
    # make `amend --correlated` a shortcut to the main command
    amendflag = correlated
    # truncate descriptions after 50 characters
    maxdescwidth = 50

    [color]
    absorb.description = yellow
    absorb.node = blue bold
    absorb.path = bold
"""

import bisect
import collections
import io

import bindings

from sapling import (
    cmdutil,
    commands,
    context,
    crecord,
    error,
    extensions,
    identity,
    mdiff,
    node,
    patch,
    registrar,
    scmutil,
    util,
)
from sapling.i18n import _, _n

testedwith = "ships-with-fb-ext"

cmdtable = {}
command = registrar.command(cmdtable)


colortable = {
    "absorb.description": "yellow",
    "absorb.node": "blue bold",
    "absorb.path": "bold",
}

defaultdict = collections.defaultdict


class nullui:
    """blank ui object doing nothing"""

    debugflag = False
    verbose = False
    quiet = True

    def __getitem__(name):
        def nullfunc(*args, **kwds):
            return

        return nullfunc


class emptyfilecontext:
    """minimal filecontext representing an empty file"""

    def data(self):
        return b""

    def node(self):
        return node.nullid


def uniq(lst):
    """list -> list. remove duplicated items without changing the order"""
    seen = set()
    result = []
    for x in lst:
        if x not in seen:
            seen.add(x)
            result.append(x)
    return result


def getdraftstack(headctx, limit=None):
    """(ctx, int?) -> [ctx]. get a linear stack of non-public changesets.

    changesets are sorted in topo order, oldest first.
    return at most limit items, if limit is a positive number.

    merges are considered as non-draft as well. i.e. every commit
    returned has and only has 1 parent.
    """
    ctx = headctx
    result = []
    while not ctx.ispublic() and not ctx.obsolete():
        if limit and len(result) >= limit:
            break
        if len(ctx.parents()) > 1:
            break
        result.append(ctx)
        ctx = ctx.p1()
    result.reverse()
    return result


def getfilestack(stack, path, seenfctxs=None):
    """([ctx], str, set) -> [fctx], {ctx: fctx}

    stack is a list of contexts, from old to new. usually they are what
    "getdraftstack" returns.

    follows renames, but not copies.

    seenfctxs is a set of filecontexts that will be considered "immutable".
    they are usually what this function returned in earlier calls, useful
    to avoid issues that a file was "moved" to multiple places and was then
    modified differently, like: "a" was copied to "b", "a" was also copied to
    "c" and then "a" was deleted, then both "b" and "c" were "moved" from "a"
    and we enforce only one of them to be able to affect "a"'s content.

    return an empty list and an empty dict, if the specified path does not
    exist in stack[-1] (the top of the stack).

    otherwise, return a list of de-duplicated filecontexts, and the map to
    convert ctx in the stack to fctx, for possible mutable fctxs. the first item
    of the list would be outside the stack and should be considered immutable.
    the remaining items are within the stack.

    for example, given the following changelog and corresponding filelog
    revisions:

      changelog: 3----4----5----6----7
      filelog:   x    0----1----1----2 (x: no such file yet)

    - if stack = [5, 6, 7], returns ([0, 1, 2], {5: 1, 6: 1, 7: 2})
    - if stack = [3, 4, 5], returns ([e, 0, 1], {4: 0, 5: 1}), where "e" is a
      dummy empty filecontext.
    - if stack = [2], returns ([], {})
    - if stack = [7], returns ([1, 2], {7: 2})
    - if stack = [6, 7], returns ([1, 2], {6: 1, 7: 2}), although {6: 1} can be
      removed, since 1 is immutable.
    """
    assert stack

    if seenfctxs is None:
        seenfctxs = set()

    if path not in stack[-1]:
        return [], {}

    fctxs = []
    fctxmap = {}

    pctx = stack[0].p1()  # the public (immutable) ctx we stop at
    for ctx in reversed(stack):
        if path not in ctx:  # the file is added in the next commit
            pctx = ctx
            break
        fctx = ctx[path]
        fctxs.append(fctx)
        if fctx in seenfctxs:  # treat fctx as the immutable one
            pctx = None  # do not add another immutable fctx
            break
        fctxmap[ctx] = fctx  # only for mutable fctxs
        renamed = fctx.renamed()
        if renamed:
            path = renamed[0]  # follow rename
            if path in ctx:  # but do not follow copy
                pctx = ctx.p1()
                break

    if pctx is not None:  # need an extra immutable fctx
        if path in pctx:
            fctxs.append(pctx[path])
        else:
            fctxs.append(emptyfilecontext())

    fctxs.reverse()
    # note: we rely on a property of hg: filerev is not reused for linear
    # history. i.e. it's impossible to have:
    #   changelog:  4----5----6 (linear, no merges)
    #   filelog:    1----2----1
    #                         ^ reuse filerev (impossible)
    # because parents are part of the hash. if that's not true, we need to
    # remove uniq and find a different way to identify fctxs.
    return uniq(fctxs), fctxmap


def overlaycontext(memworkingcopy, ctx, parents=None, date=None):
    """({path: content}, ctx, (p1node, p2node)?, {}?) -> memctx
    memworkingcopy overrides file contents.
    """
    mctx = context.memctx.mirrorformutation(
        ctx,
        "absorb",
        parents=parents,
        date=date,
    )
    for path, data in memworkingcopy.items():
        fctx = context.overlayfilectx(ctx[path], datafunc=lambda data=data: data)
        mctx[path] = fctx
    return mctx


class filefixupstate:
    """state needed to apply fixups to a single file

    internally, it keeps file contents of several revisions and a linelog.

    the linelog uses odd revision numbers for original contents (fctxs passed
    to __init__), and even revision numbers for fixups, like:

        linelog rev 1: self.fctxs[0] (from an immutable "public" changeset)
        linelog rev 2: fixups made to self.fctxs[0]
        linelog rev 3: self.fctxs[1] (a child of fctxs[0])
        linelog rev 4: fixups made to self.fctxs[1]
        ...

    a typical use is like:

        1. call diffwith, to calculate self.fixups
        2. (optionally), present self.fixups to the user, or change it
        3. call apply, to apply changes
        4. read results from "finalcontents", or call getfinalcontent
    """

    def __init__(self, fctxs, path, ui=None, opts=None):
        """([fctx], ui or None) -> None

        fctxs should be linear, and sorted by topo order - oldest first.
        fctxs[0] will be considered as "immutable" and will not be changed.
        """
        self.fctxs = fctxs
        self.path = path
        self.ui = ui or nullui()
        self.opts = opts or {}

        # following fields are built from fctxs. they exist for perf reason
        self.contents = [f.data() for f in fctxs]
        self.contentlines = list(map(mdiff.splitnewlines, self.contents))
        self.linelog = self._buildlinelog()
        if self.ui.debugflag:
            assert self._checkoutlinelog() == self.contents

        # used for _iscontinuous check
        self.gap_lines = _calculate_gap_lines(self.linelog)

        # following fields will be filled later
        self.chunkstats = [0, 0]  # [adopted, total : int]
        self.targetlines = []  # [str]
        self.fixups = []  # [(linelog rev, a1, a2, b1, b2)]
        self.finalcontents = []  # [str]
        self.ctxaffected = set()

    def diffwith(self, targetfctx, fm=None):
        """calculate fixups needed by examining the differences between
        self.fctxs[-1] and targetfctx, chunk by chunk.

        targetfctx is the target state we move towards. we may or may not be
        able to get there because not all modified chunks can be amended into
        a non-public fctx unambiguously.

        call this only once, before apply().

        update self.fixups, self.chunkstats, and self.targetlines.
        """
        a = self.contents[-1]
        alines = self.contentlines[-1]
        b = targetfctx.data()
        blines = mdiff.splitnewlines(b)
        self.targetlines = blines

        # [(rev, linenum, pc, deleted)]
        annotated = self.linelog.checkout_lines(self.linelog.max_rev())
        # change the last dummy line to belong to the last actual line
        if len(annotated) > 1:
            annotated[-1] = (annotated[-2][0], annotated[-2][1], *annotated[-1][2:])
        assert len(annotated) == len(alines) + 1

        # analyse diff blocks
        for chunk in self._alldiffchunks(a, b, alines, blines):
            newfixups = self._analysediffchunk(chunk, annotated)
            self.chunkstats[0] += bool(newfixups)  # 1 or 0
            self.chunkstats[1] += 1
            self.fixups += newfixups
            if fm is not None:
                self._showchanges(fm, alines, blines, chunk, newfixups)

    def apply(self):
        """apply self.fixups. update self.linelog, self.finalcontents.

        call this only once, before getfinalcontent(), after diffwith().
        """
        max_rev = self.linelog.max_rev()
        for rev, a1, a2, b1, b2 in reversed(self.fixups):
            blines = self.targetlines[b1:b2]
            if self.ui.debugflag:
                idx = (max(rev - 1, 0)) // 2
                self.ui.write(
                    _("%s: chunk %d:%d -> %d lines\n")
                    % (node.short(self.fctxs[idx].node()), a1, a2, len(blines))
                )
            self.linelog = self.linelog.edit_chunk(max_rev, a1, a2, rev, b1, b2)
        if self.opts.get("edit_lines", False):
            self.finalcontents = self._checkoutlinelogwithedits()
        else:
            self.finalcontents = self._checkoutlinelog()

    def getfinalcontent(self, fctx):
        """(fctx) -> str. get modified file content for a given filecontext"""
        idx = self.fctxs.index(fctx)
        return self.finalcontents[idx]

    def _analysediffchunk(self, chunk, annotated):
        """analyse a different chunk and return new fixups found

        return [] if no lines from the chunk can be safely applied.

        the chunk (or lines) cannot be safely applied, if, for example:
          - the modified (deleted) lines belong to a public changeset
            (self.fctxs[0])
          - the chunk is a pure insertion and the adjacent lines (at most 2
            lines) belong to different non-public changesets, or do not belong
            to any non-public changesets.
          - the chunk is modifying lines from different changesets.
            in this case, if the number of lines deleted equals to the number
            of lines added, assume it's a simple 1:1 map (could be wrong).
            otherwise, give up.
          - the chunk is modifying lines from a single non-public changeset,
            but other revisions touch the area as well. i.e. the lines are
            not continuous as seen from the linelog.
        """
        a1, a2, b1, b2 = chunk
        # find involved indexes from annotate result
        involved = annotated[a1:a2]
        if not involved and annotated:  # a1 == a2 and a is not empty
            # pure insertion, check nearby lines. ignore lines belong
            # to the public (first) changeset (i.e. annotated[i][0] == 1)
            nearbylinenums = set([a2, max(0, a1 - 1)])
            involved = [annotated[i] for i in nearbylinenums if annotated[i][0] != 1]
        involvedrevs = list(set(r[0] for r in involved))
        newfixups = []
        if len(involvedrevs) == 1 and self._iscontinuous(a1, a2 - 1):
            # chunk belongs to a single revision
            rev = involvedrevs[0]
            if rev > 1:
                fixuprev = rev + 1
                newfixups.append((fixuprev, a1, a2, b1, b2))
        elif a2 - a1 == b2 - b1 or b1 == b2:
            # 1:1 line mapping, or chunk was deleted
            for i in range(a1, a2):
                rev, linenum = annotated[i][:2]
                if rev > 1:
                    if b1 == b2:  # deletion, simply remove that single line
                        nb1 = nb2 = 0
                    else:  # 1:1 line mapping, change the corresponding rev
                        nb1 = b1 + i - a1
                        nb2 = nb1 + 1
                    fixuprev = rev + 1
                    newfixups.append((fixuprev, i, i + 1, nb1, nb2))
        return self._optimizefixups(newfixups)

    @staticmethod
    def _alldiffchunks(a, b, alines, blines):
        """like mdiff.allblocks, but only care about differences"""
        blocks = mdiff.allblocks(a, b, lines1=alines, lines2=blines)
        for chunk, btype in blocks:
            if btype != "!":
                continue
            yield chunk

    def _buildlinelog(self):
        """calculate the initial linelog based on self.content{,line}s.
        this is similar to running a partial "annotate".
        """
        llog = bindings.linelog.IntLineLog()
        a, alines = b"", []
        for i in range(len(self.contents)):
            b, blines = self.contents[i], self.contentlines[i]
            llrev = i * 2 + 1
            chunks = self._alldiffchunks(a, b, alines, blines)
            for a1, a2, b1, b2 in reversed(list(chunks)):
                llog = llog.edit_chunk(llrev, a1, a2, llrev, b1, b2)
            a, alines = b, blines
        return llog

    def _checkoutlinelog(self):
        """() -> [str]. check out file contents from linelog"""
        contents = []
        for i in range(len(self.contents)):
            rev = (i + 1) * 2
            annotated = self.linelog.checkout_lines(rev)[:-1]
            content = b"".join(map(self._getline, annotated))
            contents.append(content)
        return contents

    def _checkoutlinelogwithedits(self):
        """() -> [str]. prompt all lines for edit"""
        max_rev = self.linelog.max_rev()
        # discard the "end" line
        alllines = self.linelog.checkout_lines(max_rev, 0)[:-1]
        # header
        editortext = _(
            '{0}: editing {1}\n{0}: "y" means the line to the right '
            "exists in the changeset to the top\n{0}:\n"
        ).format(identity.tmplprefix(), self.fctxs[-1].path())
        # [(idx, fctx)]. hide the dummy emptyfilecontext
        visiblefctxs = [
            (i, f)
            for i, f in enumerate(self.fctxs)
            if not isinstance(f, emptyfilecontext)
        ]
        for i, (j, f) in enumerate(visiblefctxs):
            editortext += _("%s: %s/%s %s %s\n") % (
                identity.tmplprefix(),
                "|" * i,
                "-" * (len(visiblefctxs) - i + 1),
                node.short(f.node()),
                f.description().split("\n", 1)[0],
            )
        editortext += _("%s: %s\n") % (identity.tmplprefix(), "|" * len(visiblefctxs))
        # figure out the lifetime of a line, this is relatively inefficient,
        # but probably fine
        lineset = defaultdict(lambda: set())  # {(llrev, linenum): {llrev}}
        for i, f in visiblefctxs:
            annotated = self.linelog.checkout_lines((i + 1) * 2)
            for info in annotated:
                l = tuple(info[:2])
                lineset[l].add(i)
        # append lines
        for info in alllines:
            l = tuple(info[:2])
            editortext += "    %s : %s" % (
                "".join([("y" if i in lineset[l] else " ") for i, _f in visiblefctxs]),
                self._getline(l).decode(),
            )
        # run editor
        editedtext = self.ui.edit(editortext, "", action="absorb")
        if not editedtext:
            raise error.Abort(_("empty editor text"))
        # parse edited result
        contents = [b"" for i in self.fctxs]
        leftpadpos = 4
        colonpos = leftpadpos + len(visiblefctxs) + 1
        for l in editedtext.splitlines(True):
            if l.startswith(f"{identity.tmplprefix()}:"):
                continue
            if l[colonpos - 1 : colonpos + 2] != " : ":
                raise error.Abort(_("malformed line: %s") % l)
            linecontent = l[colonpos + 2 :].encode()
            for i, ch in enumerate(l[leftpadpos : colonpos - 1]):
                if ch == "y":
                    contents[visiblefctxs[i][0]] += linecontent
        # chunkstats is hard to calculate if anything changes, therefore
        # set them to just a simple value (1, 1).
        if editedtext != editortext:
            self.chunkstats = [1, 1]
        return contents

    def _getline(self, lineinfo):
        """((rev, linenum)) -> str. convert rev+line number to line content"""
        rev, linenum = lineinfo[:2]
        if rev & 1:  # odd: original line taken from fctxs
            return self.contentlines[rev // 2][linenum]
        else:  # even: fixup line from targetfctx
            return self.targetlines[linenum]

    def _iscontinuous(self, a1, a2):
        """(a1, a2 : int) -> bool

        check if these lines are continuous. i.e. no other insertions or
        deletions (from other revisions) among these lines.
        """
        if a1 >= a2:
            return True
        gap_index = bisect.bisect_left(self.gap_lines, a1)
        if gap_index >= len(self.gap_lines):
            return True
        gap = self.gap_lines[gap_index]
        return gap >= a2

    def _optimizefixups(self, fixups):
        """[(rev, a1, a2, b1, b2)] -> [(rev, a1, a2, b1, b2)].
        merge adjacent fixups to make them less fragmented.
        """
        result = []
        pcurrentchunk = [[-1, -1, -1, -1, -1]]

        def pushchunk():
            if pcurrentchunk[0][0] != -1:
                result.append(tuple(pcurrentchunk[0]))

        for i, chunk in enumerate(fixups):
            rev, a1, a2, b1, b2 = chunk
            lastrev = pcurrentchunk[0][0]
            lasta2 = pcurrentchunk[0][2]
            lastb2 = pcurrentchunk[0][4]
            if (
                a1 == lasta2
                and b1 == lastb2
                and rev == lastrev
                and self._iscontinuous(max(a1 - 1, 0), a1)
            ):
                # merge into currentchunk
                pcurrentchunk[0][2] = a2
                pcurrentchunk[0][4] = b2
            else:
                pushchunk()
                pcurrentchunk[0] = list(chunk)
        pushchunk()
        return result

    def _showchanges(self, fm, alines, blines, chunk, fixups):
        def trim(line):
            if line.endswith(b"\n"):
                line = line[:-1]
            return line

        # this is not optimized for perf but _showchanges only gets executed
        # with an extra command-line flag.
        a1, a2, b1, b2 = chunk
        aidxs, bidxs = [0] * (a2 - a1), [0] * (b2 - b1)
        for idx, fa1, fa2, fb1, fb2 in fixups:
            for i in range(fa1, fa2):
                aidxs[i - a1] = (max(idx, 1) - 1) // 2
            for i in range(fb1, fb2):
                bidxs[i - b1] = (max(idx, 1) - 1) // 2

        fm.startitem()
        fm.write(
            "hunk",
            "        %s\n",
            "@@ -%d,%d +%d,%d @@" % (a1, a2 - a1, b1, b2 - b1),
            label="diff.hunk",
        )
        fm.data(path=self.path, linetype="hunk")

        def writeline(idx, diffchar, line, linetype, linelabel):
            fm.startitem()
            node = ""
            if idx:
                ctx = self.fctxs[idx].changectx()
                fm.context(ctx=ctx)
                node = ctx.hex()
                self.ctxaffected.add(ctx)
            fm.write("node", "%-7.7s ", node, label="absorb.node")
            fm.writebytes(
                "diffchar " + linetype, b"%s%s\n", diffchar, line, label=linelabel
            )
            fm.data(path=self.path, linetype=linetype)

        for i in range(a1, a2):
            writeline(aidxs[i - a1], b"-", trim(alines[i]), "deleted", "diff.deleted")
        for i in range(b1, b2):
            writeline(bidxs[i - b1], b"+", trim(blines[i]), "inserted", "diff.inserted")


class fixupstate:
    """state needed to run absorb

    internally, it keeps paths and filefixupstates.

    a typical use is like filefixupstates:

        1. call diffwith, to calculate fixups
        2. (optionally), present fixups to the user, or edit fixups
        3. call apply, to apply changes to memory
        4. call commit, to commit changes to hg database
    """

    def __init__(self, stack, ui=None, opts=None):
        """([ctx], ui or None) -> None

        stack: should be linear, and sorted by topo order - oldest first.
        all commits in stack are considered mutable.
        """
        assert stack
        self.ui = ui or nullui()
        self.opts = opts or {}
        self.stack = stack
        self.repo = stack[-1].repo()
        self.checkoutidentifier = self.repo.dirstate.checkoutidentifier

        # following fields will be filled later
        self.paths = []  # [str]
        self.status = None  # ctx.status output
        self.fctxmap = {}  # {path: {ctx: fctx}}
        self.fixupmap = {}  # {path: filefixupstate}
        self.replacemap = {}  # {oldnode: newnode or None}
        self.finalnode = None  # head after all fixups
        self.ctxaffected = set()  # ctx that will be absorbed into

    def diffwith(self, targetctx, match=None, fm=None):
        """diff and prepare fixups. update self.fixupmap, self.paths"""
        # only care about modified files
        self.status = self.stack[-1].status(targetctx, match)
        self.paths = []
        # but if --edit-lines is used, the user may want to edit files
        # even if they are not modified
        editopt = self.opts.get("edit_lines")
        if not self.status.modified and editopt and match:
            interestingpaths = match.files()
        else:
            interestingpaths = self.status.modified
        # prepare the filefixupstate
        seenfctxs = set()
        # sorting is necessary to eliminate ambiguity for the "double move"
        # case: "hg cp A B; hg cp A C; hg rm A", then only "B" can affect "A".
        for path in sorted(interestingpaths):
            if self.ui.debugflag:
                self.ui.write(_("calculating fixups for %s\n") % path)
            targetfctx = targetctx[path]
            fctxs, ctx2fctx = getfilestack(self.stack, path, seenfctxs)
            # ignore symbolic links or binary, or unchanged files
            if any(
                f.islink() or util.binary(f.data())
                for f in [targetfctx] + fctxs
                if not isinstance(f, emptyfilecontext)
            ):
                continue
            if targetfctx.data() == fctxs[-1].data() and not editopt:
                continue
            seenfctxs.update(fctxs[1:])
            self.fctxmap[path] = ctx2fctx
            fstate = filefixupstate(fctxs, path, ui=self.ui, opts=self.opts)
            if fm is not None:
                fm.startitem()
                fm.plain("showing changes for ")
                fm.write("path", "%s\n", path, label="absorb.path")
                fm.data(linetype="path")
            fstate.diffwith(targetfctx, fm)
            self.fixupmap[path] = fstate
            self.paths.append(path)
            self.ctxaffected.update(fstate.ctxaffected)

    def apply(self):
        """apply fixups to individual filefixupstates"""
        for path, state in self.fixupmap.items():
            if self.ui.debugflag:
                self.ui.write(_("applying fixups to %s\n") % path)
            state.apply()

    @property
    def chunkstats(self):
        """-> {path: chunkstats}. collect chunkstats from filefixupstates"""
        return dict((path, state.chunkstats) for path, state in self.fixupmap.items())

    def commit(self):
        """commit changes. update self.finalnode, self.replacemap"""
        with self.repo.wlock(), self.repo.lock():
            with self.repo.transaction("absorb"):
                self._commitstack()
                replacements = {
                    old: [new] if new else [] for old, new in self.replacemap.items()
                }
                moves = scmutil.cleanupnodes(self.repo, replacements, "absorb")
                newwd = moves.get(self.repo["."].node())
                if newwd is not None:
                    self._moveworkingdirectoryparent(newwd)
        return self.finalnode

    def printchunkstats(self):
        """print things like '1 of 2 chunk(s) applied'"""
        ui = self.ui
        chunkstats = self.chunkstats
        if ui.verbose:
            # chunkstats for each file
            for path, stat in chunkstats.items():
                if stat[0]:
                    ui.write(
                        _n(
                            "%s: %d of %d chunk applied\n",
                            "%s: %d of %d chunks applied\n",
                            stat[1],
                        )
                        % (path, stat[0], stat[1])
                    )
        elif not ui.quiet:
            # a summary for all files
            stats = chunkstats.values()
            applied, total = (sum(s[i] for s in stats) for i in (0, 1))
            if applied == 0:
                ui.write(_("nothing applied\n"))
            else:
                ui.write(
                    _n("%d of %d chunk applied\n", "%d of %d chunks applied\n", total)
                    % (applied, total)
                )

    def _commitstack(self):
        """make new commits. update self.finalnode, self.replacemap.
        it is split from "commit" to avoid too much indentation.
        """
        # last node (20-char) committed by us
        lastcommitted = None
        # p1 which overrides the parent of the next commit, "None" means use
        # the original parent unchanged
        nextp1 = None
        for ctx in self.stack:
            memworkingcopy = self._getnewfilecontents(ctx)
            if not memworkingcopy and not lastcommitted:
                # nothing changed, nothing committed
                nextp1 = ctx
                continue
            msg = ""
            if self._willbecomenoop(memworkingcopy, ctx, nextp1):
                # changeset is no longer necessary
                self.replacemap[ctx.node()] = None
                msg = _("became empty and was dropped")
            else:
                # changeset needs re-commit
                nodestr = self._commitsingle(memworkingcopy, ctx, p1=nextp1)
                lastcommitted = self.repo[nodestr]
                nextp1 = lastcommitted
                self.replacemap[ctx.node()] = lastcommitted.node()
                if memworkingcopy:
                    msg = _("%d file(s) changed, became %s") % (
                        len(memworkingcopy),
                        self._ctx2str(lastcommitted),
                    )
                else:
                    msg = _("became %s") % self._ctx2str(lastcommitted)
            if self.ui.verbose and msg:
                self.ui.write(_("%s: %s\n") % (self._ctx2str(ctx), msg))
        self.finalnode = lastcommitted and lastcommitted.node()

    def _ctx2str(self, ctx):
        if self.ui.debugflag:
            return ctx.hex()
        else:
            return node.short(ctx.node())

    def _getnewfilecontents(self, ctx):
        """(ctx) -> {path: str}

        fetch file contents from filefixupstates.
        return the working copy overrides - files different from ctx.
        """
        result = {}
        for path in self.paths:
            ctx2fctx = self.fctxmap[path]  # {ctx: fctx}
            if ctx not in ctx2fctx:
                continue
            fctx = ctx2fctx[ctx]
            content = fctx.data()
            newcontent = self.fixupmap[path].getfinalcontent(fctx)
            if content != newcontent:
                result[fctx.path()] = newcontent
        return result

    def _moveworkingdirectoryparent(self, node):
        dirstate = self.repo.dirstate
        ctx = self.repo[node]
        with dirstate.parentchange():
            dirstate.rebuild(node, ctx.manifest(), self.paths, exact=True)

    @staticmethod
    def _willbecomenoop(memworkingcopy, ctx, pctx=None):
        """({path: content}, ctx, ctx) -> bool. test if a commit will be noop

        if it will become an empty commit (does not change anything, after the
        memworkingcopy overrides), return True. otherwise return False.
        """
        if not pctx:
            parents = ctx.parents()
            if len(parents) != 1:
                return False
            pctx = parents[0]
        # ctx changes more files (not a subset of memworkingcopy)
        if not set(ctx.files()).issubset(set(memworkingcopy.keys())):
            return False
        for path, content in memworkingcopy.items():
            if path not in pctx or path not in ctx:
                return False
            fctx = ctx[path]
            pfctx = pctx[path]
            if pfctx.flags() != fctx.flags():
                return False
            if pfctx.data() != content:
                return False
        return True

    def _commitsingle(self, memworkingcopy, ctx, p1=None):
        """(ctx, {path: content}, node) -> node. make a single commit

        the commit is a clone from ctx, with a (optionally) different p1, and
        different file contents replaced by memworkingcopy.
        """
        parents = p1 and [self.repo[p1]]

        mctx = overlaycontext(
            memworkingcopy,
            ctx,
            parents,
            date=self.opts["date"],
        )
        # preserve phase
        with mctx.repo().ui.configoverride({("phases", "new-commit"): ctx.phase()}):
            return mctx.commit()


def _parsechunk(hunk):
    """(crecord.uihunk or patch.recordhunk) -> (path, (a1, a2, [bline]))"""
    if type(hunk) not in (crecord.uihunk, patch.recordhunk):
        return None, None
    path = hunk.header.filename()
    a1 = hunk.fromline + len(hunk.before) - 1
    # remove before and after context
    hunk.before = hunk.after = []
    buf = io.BytesIO()
    hunk.write(buf)
    patchlines = mdiff.splitnewlines(buf.getvalue())
    # hunk.prettystr() will update hunk.removed
    a2 = a1 + hunk.removed
    blines = [l[1:] for l in patchlines[1:] if l[0:1] != b"-"]
    return path, (a1, a2, blines)


def overlaydiffcontext(ctx, chunks):
    """(ctx, [crecord.uihunk]) -> memctx

    return a memctx with some [1] patches (chunks) applied to ctx.
    [1]: modifications are handled. renames, mode changes, etc. are ignored.
    """
    # sadly the applying-patch logic is hardly reusable, and messy:
    # 1. the core logic "_applydiff" is too heavy - it writes .rej files, it
    #    needs a file stream of a patch and will re-parse it, while we have
    #    structured hunk objects at hand.
    # 2. a lot of different implementations about "chunk" (patch.hunk,
    #    patch.recordhunk, crecord.uihunk)
    # as we only care about applying changes to modified files, no mode
    # change, no binary diff, and no renames, it's probably okay to
    # re-invent the logic using much simpler code here.
    memworkingcopy = {}  # {path: content}
    patchmap = defaultdict(lambda: [])  # {path: [(a1, a2, [bline])]}
    for path, info in map(_parsechunk, chunks):
        if not path or not info:
            continue
        patchmap[path].append(info)
    for path, patches in patchmap.items():
        if path not in ctx or not patches:
            continue
        patches.sort(reverse=True)
        lines = mdiff.splitnewlines(ctx[path].data())
        for a1, a2, blines in patches:
            lines[a1:a2] = blines
        memworkingcopy[path] = b"".join(lines)
    return overlaycontext(memworkingcopy, ctx)


def absorb(ui, repo, stack=None, targetctx=None, pats=None, opts=None):
    """pick fixup chunks from targetctx, apply them to stack.

    if targetctx is None, the working copy context will be used.
    if stack is None, the current draft stack will be used.
    return fixupstate.
    """
    if stack is None:
        limit = ui.configint("absorb", "maxstacksize", 50)
        stack = getdraftstack(repo["."], limit)
        if limit and len(stack) >= limit:
            ui.warn(
                _("absorb: only the recent %d changesets will be analysed\n") % limit
            )
    if not stack:
        raise error.Abort(_("no changeset to change"))
    if targetctx is None:  # default to working copy
        targetctx = repo[None]
    if pats is None:
        pats = ()
    if opts is None:
        opts = {}

    date = opts.get("date")
    if date:
        opts["date"] = util.parsedate(date)
    else:
        opts["date"] = None

    state = fixupstate(stack, ui=ui, opts=opts)
    matcher = scmutil.match(targetctx, pats, opts)
    if opts.get("interactive"):
        diff = patch.diff(repo, stack[-1], targetctx, matcher)
        origchunks = patch.parsepatch(diff)
        chunks = cmdutil.recordfilter(ui, origchunks)[0]
        targetctx = overlaydiffcontext(stack[-1], chunks)
    fm = None
    if not (ui.quiet and opts.get("apply_changes")) and not opts.get("edit_lines"):
        fm = ui.formatter("absorb", opts)
    state.diffwith(targetctx, matcher, fm)
    if fm is not None and state.ctxaffected:
        fm.startitem()
        count = len(state.ctxaffected)
        fm.write(
            "count",
            _n("\n%d changeset affected\n", "\n%d changesets affected\n", count),
            count,
        )
        fm.data(linetype="summary")
        for ctx in reversed(stack):
            if ctx not in state.ctxaffected:
                continue
            fm.startitem()
            fm.context(ctx=ctx)
            fm.data(linetype="changeset")
            fm.write("node", "%-7.7s ", ctx.hex(), label="absorb.node")
            descfirstline = ctx.description().splitlines()[0]
            fm.write("descfirstline", "%s\n", descfirstline, label="absorb.description")
        fm.end()
    if not opts.get("edit_lines") and not any(
        f.fixups for f in state.fixupmap.values()
    ):
        ui.write(_("nothing to absorb\n"))
    elif not opts.get("dry_run"):
        if not opts.get("apply_changes"):
            if ui.promptchoice("apply changes (yn)? $$ &Yes $$ &No", default=0):
                raise error.Abort(_("absorb cancelled\n"))
        state.apply()
        state.commit()
        state.printchunkstats()
    return state


@command(
    "absorb|ab||sf",
    [
        (
            "a",
            "apply-changes",
            None,
            _("apply changes without prompting for confirmation"),
        ),
        (
            "p",
            "print-changes",
            None,
            _("print which commits are modified by which changes (DEPRECATED)"),
        ),
        (
            "i",
            "interactive",
            None,
            _("interactively select which chunks to apply (EXPERIMENTAL)"),
        ),
        (
            "e",
            "edit-lines",
            None,
            _(
                "edit what lines belong to which commits before commit "
                "(EXPERIMENTAL)"
            ),
        ),
        ("d", "date", "", _("record the specified date as commit date"), _("DATE")),
    ]
    + commands.dryrunopts
    + commands.templateopts
    + commands.walkopts,
    _("@prog@ absorb [OPTION] [FILE]..."),
    legacyaliases=["abs", "abso", "absor"],
)
def absorbcmd(ui, repo, *pats, **opts):
    """intelligently integrate pending changes into current stack

    Attempt to amend each pending change to the proper commit in your
    stack. Absorb does not write to the working copy.

    If absorb cannot find an unambiguous commit to amend for a change, that
    change will be left in the working copy, untouched. The unabsorbed
    changes can be observed by :prog:`status` or :prog:`diff` afterwards.

    Commits outside the revset `::. and not public() and not merge()` will
    not be changed.

    Commits that become empty after applying the changes will be deleted.

    By default, absorb will show what it plans to do and prompt for
    confirmation.  If you are confident that the changes will be absorbed
    to the correct place, run :prog:`absorb -a` to apply the changes
    immediately.

    Returns 0 if anything was absorbed, 1 if nothing was absorbed.
    """
    state = absorb(ui, repo, pats=pats, opts=opts)
    if sum(s[0] for s in state.chunkstats.values()) == 0:
        return 1


def _wrapamend(flag):
    """add flag to amend, which will be a shortcut to the absorb command"""
    if not flag:
        return
    amendcmd = extensions.bind(_amendcmd, flag)
    # the amend command can exist in amend, or evolve
    for extname in ["amend", None]:
        try:
            if extname is None:
                cmdtable = commands.table
            else:
                ext = extensions.find(extname)
                cmdtable = ext.cmdtable
        except (KeyError, AttributeError):
            continue
        try:
            entry = extensions.wrapcommand(cmdtable, "amend", amendcmd)
            options = entry[1]
            msg = _(
                "incorporate corrections into stack. "
                "see '@prog@ help absorb' for details"
            )
            options.append(("", flag, None, msg))
            return
        except error.UnknownCommand:
            pass


def _amendcmd(flag, orig, ui, repo, *pats, **opts):
    if not opts.get(flag):
        return orig(ui, repo, *pats, **opts)
    # use absorb
    for k, v in opts.items():  # check unsupported flags
        if v and k not in ["interactive", flag]:
            raise error.Abort(
                _("--%s does not support --%s") % (flag, k.replace("_", "-"))
            )
    state = absorb(ui, repo, pats=pats, opts=opts)
    # different from the original absorb, tell users what chunks were
    # ignored and were left. it's because users usually expect "amend" to
    # take all of their changes and will feel strange otherwise.
    # the original "absorb" command faces more-advanced users knowing
    # what's going on and is less verbose.
    adoptedsum = 0
    messages = []
    for path, (adopted, total) in state.chunkstats.items():
        adoptedsum += adopted
        if adopted == total:
            continue
        reason = _("%d modified chunks were ignored") % (total - adopted)
        messages.append(("M", "modified", path, reason))
    for idx, word, symbol in [
        (0, "modified", "M"),
        (1, "added", "A"),
        (2, "removed", "R"),
        (3, "deleted", "!"),
    ]:
        paths = set(state.status[idx]) - set(state.paths)
        for path in sorted(paths):
            if word == "modified":
                reason = _("unsupported file type (ex. binary or link)")
            else:
                reason = _("%s files were ignored") % word
            messages.append((symbol, word, path, reason))
    if messages:
        ui.write(_("\n# changes not applied and left in working copy:\n"))
        for symbol, word, path, reason in messages:
            ui.write(
                _("# %s %s : %s\n")
                % (
                    ui.label(symbol, "status." + word),
                    ui.label(path, "status." + word),
                    reason,
                )
            )

    if adoptedsum == 0:
        return 1


def _calculate_gap_lines(linelog, rev=None):
    if rev is None:
        rev = linelog.max_rev()
    all_lines = linelog.checkout_lines(rev, 0)
    gap_lines = []
    lineno = -1
    last_gap_line = -1
    for _rev, _lineno, _pc, deleted in all_lines:
        if deleted and lineno != last_gap_line and lineno >= 0:
            last_gap_line = lineno
            gap_lines.append(lineno + 0.5)
        else:
            lineno += 1
    return gap_lines


def extsetup(ui):
    _wrapamend(ui.config("absorb", "amendflag"))
