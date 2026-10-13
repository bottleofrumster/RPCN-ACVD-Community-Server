ACVD COMMUNITY CLIENT

Requires RPCS3, your RPCN account, and ACVD BLUS31194 with update v1.02.
Use the kit in "client user patch files".
The endpoint is a placeholder. Before copying files, run from the project root:
   python3 client/configure_endpoint.py SERVER_IPV4
Obtain SERVER_IPV4 from the operator. Do not publish the configured copy.
This changes addresses only. A different host also needs matching public keys.

INSTALL
1. Close RPCS3. Back up savedata, patch.bnd, system/publickey.pem and
   patches/BLUS31194_patch.yml if present.
2. Copy from the kit:
   - RPCS3/patches/BLUS31194_patch.yml to RPCS3/patches.
   - RPCS3/dev_hdd0/game/BLUS31194/USRDIR/bind/patch.bnd to that same
     path in your RPCS3 installation. This REPLACES the selected map pack.
   - The contents of either supplied game's PS3_GAME/USRDIR to your
     game's PS3_GAME/USRDIR. Both supplied game folders are identical.
     Keep existing custommap .layout files if prompted to replace them.
3. Open RPCS3 > Manage > Game Patches. Enable:
   ACVD Community Server - Public
   ACVD Free Battle Cache Fallback
   ACVD DLC Enrollment Preservation
   Disable the ACVD LAN routing patch. Select Apply and Save.
4. Sign in to RPCN. Start ACVD.

Do not replace your existing config/patch_config.yml.
If a patch is missing, drag BLUS31194_patch.yml into Game Patches.
All players must use the same map pack. The supplied binder uses Default maps.
Regulation r1.03 downloads from the server. Do not delete REGU.DAT.

CAUTION
Do not accept a notice that replaces your savedata.
Keep backups outside RPCS3. Do not restore an old save over newer progress.
This kit has no PS3 installer. PS3 crossplay is not qualified.
The experimental RPCS3 source patch in rpcs3/ is not included in the kit.

If connection fails, check RPCN sign-in and the applied patch in RPCS3.log.
Required PPU hash: 546acba4dd056f495ece5908935cc84a5b1b17ae.
Multi-player re-sortie and delayed team presence remain known issues.
