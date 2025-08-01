# Copyright (c) Meta Platforms, Inc. and affiliates.
#
# This software may be used and distributed according to the terms of the
# GNU General Public License found in the LICENSE file in the root
# directory of this source tree.
#require slow

  $ . "${TEST_FIXTURES}/library.sh"

setup configuration
  $ setup_common_config
  $ mononoke_testtool drawdag -R repo <<'EOF'
  > A-B-C
  >    \
  >     D
  > # bookmark: C main
  > EOF
  A=aa53d24251ff3f54b1b2c29ae02826701b2abeb0079f1bb13b8434b54cd87675
  B=f8c75e41a0c4d29281df765f39de47bca1dcadfdc55ada4ccc2f6df567201658
  C=e32a1e342cdb1e38e88466b4c1a01ae9f410024017aa21dc0a1c5da6b3963bf2
  D=5a25c0a76794bbcc5180da0949a652750101597f0fbade488e611d5c0917e7be

derived-data derive:
  $ mononoke_admin derived-data -R repo derive -T unodes -T blame -B main
  $ mononoke_admin derived-data -R repo derive --rederive -T unodes -T blame -B main

derived-data exists:

Simple usage
  $ mononoke_admin derived-data -R repo exists -T unodes -i aa53d24251ff3f54b1b2c29ae02826701b2abeb0079f1bb13b8434b54cd87675
  Derived: aa53d24251ff3f54b1b2c29ae02826701b2abeb0079f1bb13b8434b54cd87675
  $ mononoke_admin derived-data -R repo exists -T fsnodes -i aa53d24251ff3f54b1b2c29ae02826701b2abeb0079f1bb13b8434b54cd87675
  Not Derived: aa53d24251ff3f54b1b2c29ae02826701b2abeb0079f1bb13b8434b54cd87675
Multiple changesets
  $ mononoke_admin derived-data -R repo exists -T unodes -i aa53d24251ff3f54b1b2c29ae02826701b2abeb0079f1bb13b8434b54cd87675 -i 5a25c0a76794bbcc5180da0949a652750101597f0fbade488e611d5c0917e7be
  Derived: aa53d24251ff3f54b1b2c29ae02826701b2abeb0079f1bb13b8434b54cd87675
  Not Derived: 5a25c0a76794bbcc5180da0949a652750101597f0fbade488e611d5c0917e7be
  $ mononoke_admin derived-data -R repo fetch -T unodes -i aa53d24251ff3f54b1b2c29ae02826701b2abeb0079f1bb13b8434b54cd87675 -i 5a25c0a76794bbcc5180da0949a652750101597f0fbade488e611d5c0917e7be
  Derived: aa53d24251ff3f54b1b2c29ae02826701b2abeb0079f1bb13b8434b54cd87675 -> RootUnodeManifestId(ManifestUnodeId(Blake2(53a2097eea133df6d1d44507695b148b785325a505129d2fbce1ccecdda9b0c2)))
  Not Derived: 5a25c0a76794bbcc5180da0949a652750101597f0fbade488e611d5c0917e7be
Bookmark
  $ mononoke_admin derived-data -R repo exists -T unodes -B main
  Derived: e32a1e342cdb1e38e88466b4c1a01ae9f410024017aa21dc0a1c5da6b3963bf2
  $ mononoke_admin derived-data -R repo fetch -T unodes -B main
  Derived: e32a1e342cdb1e38e88466b4c1a01ae9f410024017aa21dc0a1c5da6b3963bf2 -> RootUnodeManifestId(ManifestUnodeId(Blake2(a6ad350bbe00558e673cd0b1bc6121e78f65f8d86921ec8215a93aaf57c9d2b4)))
 
derived-data count-underived:

Simple usage
  $ mononoke_admin derived-data -R repo count-underived -T unodes -i aa53d24251ff3f54b1b2c29ae02826701b2abeb0079f1bb13b8434b54cd87675
  aa53d24251ff3f54b1b2c29ae02826701b2abeb0079f1bb13b8434b54cd87675: 0
Multiple changesets
  $ mononoke_admin derived-data -R repo count-underived -T unodes -i aa53d24251ff3f54b1b2c29ae02826701b2abeb0079f1bb13b8434b54cd87675 -i 5a25c0a76794bbcc5180da0949a652750101597f0fbade488e611d5c0917e7be | sort
  5a25c0a76794bbcc5180da0949a652750101597f0fbade488e611d5c0917e7be: 1
  aa53d24251ff3f54b1b2c29ae02826701b2abeb0079f1bb13b8434b54cd87675: 0
Bookmark
  $ mononoke_admin derived-data -R repo count-underived -T unodes -B main
  e32a1e342cdb1e38e88466b4c1a01ae9f410024017aa21dc0a1c5da6b3963bf2: 0

derived-data verify-manifests:

Simple usage
  $ mononoke_admin derived-data -R repo verify-manifests -T unodes -i aa53d24251ff3f54b1b2c29ae02826701b2abeb0079f1bb13b8434b54cd87675 && echo success || echo failure
  success
Multiple types 
  $ mononoke_admin derived-data -R repo verify-manifests -T unodes -T fsnodes -i 5a25c0a76794bbcc5180da0949a652750101597f0fbade488e611d5c0917e7be && echo success || echo failure
  success
Bookmark
  $ mononoke_admin derived-data -R repo verify-manifests -T unodes -B main && echo success || echo failure
  success
