#require diff echo no-eden

  $ enable extdiff
  $ setconfig ui.extdiffcmd "echo DIFF"

  $ hg init repo
  $ cd repo
  $ echo a > a
  $ hg add a
  $ hg commit -m base -d '0 0'
  $ echo b >> a

  $ sl diff
  DIFF * * (glob)
  [1]

  $ sl show
  DIFF * * (glob)
  [1]

  $ sl log -p -r .
  changeset:   0:* (glob)
  DIFF * * (glob)
  [1]
