"""Use short descriptions. Keep source text and code names unchanged."""

MEANINGS = {
    "Authentication endpoint discovery": "The client requests the authentication server address.",
    "Cryptographic handshake": "The client starts an encrypted connection.",
    "Service availability / application version": "The client checks the server status and the application version.",
    "Session-key exchange": "The client and server exchange session keys. This panel removes the secret values.",
    "RPCN ticket authentication / game endpoint": "The server checks the RPCN ticket. The reply gives the game server address.",
    "GET_MY_USER_INFO": "The client requests its player profile. Some profile fields still require tests.",
    "GET_USER_INFO_BY_PLAYER_ID": "The client requests a player profile with the specified player ID.",
    "GET_TEAM_INFO": "The client requests team data.",
    "GET_TEAM_MEMBER_COMMENT_LIST": "The client requests the Status text of team members.",
    "WAIT_FOR_USER_LOGIN": "The client waits for the game server to accept its login.",
    "SYNC_OFFLINE_DATA": "The client sends local inventory data to the server.",
    "SYNC_MONEY": "The client sends a money value to the server. The field meanings still require checks.",
    "TRADE_MONEY": "The client requests a money transfer. The server must check both player records.",
    "GET_GARAGE_CUSTOM_INFO": "The client requests its stored garage settings.",
    "REGISTER_GARAGE_CUSTOM_INFO": "The client sends garage settings to the server.",
    "SYNCHRONIZE_DLC_PARTS_LINEUP": "The client sends DLC part data. The reply format is not fully implemented.",
    "GET_PLAYER_KEY_ITEM_LIST": "The client requests key-item records. Some record meanings are not known.",
    "REGISTER_BATTLE_RESULT": "The client sends a battle result. Reward processing still requires tests.",
    "CREATE_TEAM_SESSION": "The client requests a team session.",
    "LEAVE_TEAM_SESSION": "The client leaves a team session. This action does not remove its team membership.",
    "REGISTER_PREPARE": "The client sends sortie settings and requests a match.",
    "CANCEL_PREPARE": "The client asks the server to cancel its match request.",
    "CHECK_REGULATION_DATA": "The client sends a regulation digest. The server does not yet compare this digest.",
    "SYN": "The client starts a reliable UDP connection.",
    "SYN_ACK": "The server answers the request to start a reliable UDP connection.",
    "ACK": "The sender confirms receipt of a reliable UDP packet.",
    "DAT": "This reliable UDP packet contains application data.",
    "DAT_ACK": "This packet contains application data and a receipt confirmation.",
    "HBT": "This packet keeps the reliable UDP connection active.",
    "FIN": "The sender requests an orderly end to the reliable UDP connection.",
    "FIN_ACK": "The sender confirms the request to end the connection.",
    "RST": "The sender resets the reliable UDP connection.",
    "RETRANSMIT": "The sender requests a missing reliable UDP packet.",
    "sceNpMatching2JoinRoom": "The client asks RPCN to join a room. This request does not prove that a battle started.",
    "sceNpMatching2CreateJoinRoom": "The client asks RPCN to create and join a room.",
    "sceNpMatching2LeaveRoom": "The client leaves the RPCN room. Earlier records can show the cause.",
    "sceNpSignalingActivateConnection": "The client requests a peer connection. This request does not prove that the connection is complete.",
    "DnsHook: DNS query": "The client requests a host address. This request does not prove that an HTTP reply arrived.",
}

EVIDENCE = {
    "native_proven": "A client test supports this meaning. This result does not prove that every feature works.",
    "integration_proven": "An automated protocol test supports this meaning. A full client test is still necessary.",
    "static_recovered": "Program code supports this meaning. A client test may still be necessary.",
    "inferred": "This meaning is a possible explanation. A test must confirm it.",
    "unknown": "The meaning of this code is not known.",
    "rejected": "A test showed that this explanation was not correct.",
    "observed_api": "The client log contains this API call. The call does not prove that the operation passed.",
    "event_only": "This is a runtime log entry. It is not a decoded protocol message.",
}


def description_text(entry):
    name = entry.get("name", "UNKNOWN")
    confidence = entry.get("confidence", "unknown")
    summary = MEANINGS.get(name)
    if summary is None:
        summary = (
            "The catalog has no meaning for this code."
            if confidence == "unknown"
            else "This code identifies the named message or operation. The Source Text tab contains the original description."
        )
    code = entry.get("code")
    if isinstance(code, int):
        code = hex(code)
    lines = [
        f"Name: {name}",
        f"Layer: {entry.get('layer', 'unknown')}",
        f"Code: {code or '—'}",
        "",
        summary,
        "",
        EVIDENCE.get(confidence, EVIDENCE["unknown"]),
    ]
    references = entry.get("evidence", [])
    if references:
        lines += ["", "Evidence files:", *references]
    return "\n".join(lines)
