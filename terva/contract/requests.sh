#!/usr/bin/env bash
# One curl request per invocation, for a disposable homeserver only.
# No retries, persistent state, crypto, server setup, or connector integration.
set -u
set -o pipefail

fail() { printf '%s\n' "$1" >&2; exit 2; }
need() {
    local name
    for name in "$@"; do
        [[ -n "${!name:-}" ]] || fail "Set $name explicitly."
    done
}
uri() { jq -nr --arg value "$1" '$value | @uri'; }

operation="${1:-help}"
if [[ "$operation" == help ]]; then
    printf '%s\n' \
        'Usage: requests.sh OPERATION' \
        'Requires Bash, jq, curl >= 7.87. HS is an HTTPS origin, not a full API path.' \
        'Export AS_TOKEN through your secret mechanism; never enable shell tracing.' \
        'For isolated local HTTP only, set ALLOW_LOOPBACK_HTTP=1.' \
        'All operations except versions require AS_TOKEN.' \
        'register: LOCALPART; returns no agent token with MSC4190.' \
        'put-device/get-device/delete-device: USER_ID, DEVICE_ID; no device assertion.' \
        'whoami/devices/joined-rooms: USER_ID, DEVICE_ID.' \
        'create-room: USER_ID, DEVICE_ID, ENCRYPTED=true|false.' \
        'invite: USER_ID, DEVICE_ID, ROOM_ID, INVITEE.' \
        'join/leave/members/encryption: USER_ID, DEVICE_ID, ROOM_ID.' \
        'sync: USER_ID, DEVICE_ID; optional SINCE, opaque and URL-encoded.' \
        'send: USER_ID, DEVICE_ID, ROOM_ID, TXN_ID, PLAINTEXT_ROOM matching ROOM_ID.' \
        'Mutations require ALLOW_MUTATION=1. send contains only synthetic text.' \
        'delete-device also requires CONFIRM_DELETE_DEVICE matching USER_ID/DEVICE_ID.' \
        'No automatic cleanup. HTTP failures keep their response body and exit nonzero.'
    exit 0
fi
[[ $# == 1 ]] || fail 'Pass exactly one operation; inputs are environment variables.'
case "$operation" in
    versions|register|put-device|get-device|delete-device|whoami|devices|joined-rooms|create-room|invite|join|leave|members|encryption|sync|send) ;;
    *) fail 'Unknown operation. Run requests.sh help.' ;;
esac
command -v jq >/dev/null || fail 'jq is required.'
command -v curl >/dev/null || fail 'curl is required.'
need HS
HS="${HS%/}"
if [[ ! "$HS" =~ ^https://([A-Za-z0-9.-]+|\[[0-9a-fA-F:]+\])(:[0-9]+)?$ ]]; then
    [[ "${ALLOW_LOOPBACK_HTTP:-}" == 1 && "$HS" =~ ^http://(127\.0\.0\.1|localhost|\[::1\])(:[0-9]+)?$ ]] \
        || fail 'Use an HTTPS origin, or explicitly allow a loopback HTTP origin.'
fi

method=GET
path=''
body=''
auth=1
args=(-q --silent --show-error --fail-with-body --globoff --proto '=http,https'
      --connect-timeout 5 --max-time 35 --write-out '%{stderr}HTTP %{http_code}\n')
case "$operation" in
    register|put-device|delete-device|create-room|invite|join|leave|send)
        [[ "${ALLOW_MUTATION:-}" == 1 ]] || fail 'Mutation refused. Set ALLOW_MUTATION=1 on a disposable server.' ;;
esac
if [[ "$operation" == versions ]]; then
    auth=0
    path='/_matrix/client/versions'
else
    need AS_TOKEN
    # Bearer-token grammar rejects line breaks before it reaches the header pipe.
    [[ "$AS_TOKEN" =~ ^[A-Za-z0-9._~+/-]+=*$ ]] || fail 'AS_TOKEN is not a valid bearer token.'
fi
if [[ "$operation" != versions && "$operation" != register ]]; then
    need USER_ID DEVICE_ID
    args+=(--url-query "user_id=$USER_ID")
    case "$operation" in
        put-device|get-device|delete-device) ;;
        *) args+=(--url-query "device_id=$DEVICE_ID") ;;
    esac
fi
case "$operation" in
    invite|join|leave|members|encryption|send)
        need ROOM_ID
        room="$(uri "$ROOM_ID")" || exit 2 ;;
esac
case "$operation" in
    versions) ;;
    register)
        need LOCALPART
        [[ "$LOCALPART" =~ ^[a-z0-9_]+$ ]] || fail 'Use a lowercase alphanumeric/underscore probe localpart.'
        method=POST; path='/register'
        body="$(jq -nc --arg username "$LOCALPART" \
            '{type:"m.login.application_service",username:$username,inhibit_login:true}')" || exit 2 ;;
    put-device|get-device|delete-device)
        device="$(uri "$DEVICE_ID")" || exit 2
        path="/devices/$device"
        if [[ "$operation" == put-device ]]; then
            method=PUT; body='{"display_name":"terva contract probe"}'
        elif [[ "$operation" == delete-device ]]; then
            [[ "${CONFIRM_DELETE_DEVICE:-}" == "$USER_ID/$DEVICE_ID" ]] \
                || fail 'Deletion refused. Confirm the exact USER_ID/DEVICE_ID pair.'
            method=DELETE; body='{}'
        fi ;;
    whoami) path='/account/whoami' ;;
    devices) path='/devices' ;;
    joined-rooms) path='/joined_rooms' ;;
    create-room)
        need ENCRYPTED
        [[ "$ENCRYPTED" == true || "$ENCRYPTED" == false ]] || fail 'ENCRYPTED must be true or false.'
        method=POST; path='/createRoom'
        body="$(jq -nc --argjson encrypted "$ENCRYPTED" \
            '{visibility:"private",preset:"private_chat",name:"terva contract probe",
              initial_state:(if $encrypted then
                [{type:"m.room.encryption",state_key:"",content:{algorithm:"m.megolm.v1.aes-sha2"}}]
              else [] end)}')" || exit 2 ;;
    invite)
        need INVITEE
        method=POST; path="/rooms/$room/invite"
        body="$(jq -nc --arg user "$INVITEE" '{user_id:$user}')" || exit 2 ;;
    join) method=POST; path="/join/$room"; body='{}' ;;
    leave) method=POST; path="/rooms/$room/leave"; body='{}' ;;
    members) path="/rooms/$room/joined_members" ;;
    encryption) path="/rooms/$room/state/m.room.encryption/" ;;
    sync)
        path='/sync'
        args+=(--url-query 'timeout=0' --url-query 'set_presence=offline')
        [[ -z "${SINCE:-}" ]] || args+=(--url-query "since=$SINCE") ;;
    send)
        need TXN_ID PLAINTEXT_ROOM
        [[ "$PLAINTEXT_ROOM" == "$ROOM_ID" ]] || fail 'Confirm the known plaintext ROOM_ID in PLAINTEXT_ROOM.'
        txn="$(uri "$TXN_ID")" || exit 2
        method=PUT; path="/rooms/$room/send/m.room.message/$txn"
        body='{"msgtype":"m.text","body":"terva contract probe, synthetic content only"}' ;;
esac
[[ "$operation" == versions ]] || path="/_matrix/client/v3$path"
args+=(--request "$method")
[[ -z "$body" ]] || args+=(--header 'Content-Type: application/json' --data-binary "$body")
# No redirect following, insecure TLS, token query, or automatic retries.
# curl -q ignores ~/.curlrc. The token is on stdin, not in process arguments.
if [[ "$auth" == 1 ]]; then
    printf 'Authorization: Bearer %s\n' "$AS_TOKEN" \
        | curl "${args[@]}" --header @- "$HS$path"
else
    curl "${args[@]}" "$HS$path"
fi
