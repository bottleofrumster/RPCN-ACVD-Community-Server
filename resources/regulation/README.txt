REGULATION SEED

contents_101.bin contains regulation r1.03.
publickey.pem verifies this seed. It is not a private signing key.

Run acvd-server init to verify the seed and install a copy signed for the new
host. Existing installed regulation is preserved.

The server reads data/cdn/regulation/contents_101.bin and serves:
  /regulation/contents_101.bin  Signed download manifest
  /regulation/regulation.bnd   Signed regulation archive

Clients need this host's address and PUBLIC key.
Never distribute keys/server-private.pem.

Seed SHA256:
c48348c144b820ac0ee1568c8946c022b3aa5f08b2b21a93442ed46ec2377469
Clear payload SHA256:
0e212ae3d508027df3ad00ba5fa4bb291c958f2ad8f6a3a59a631e9a4b53d363
