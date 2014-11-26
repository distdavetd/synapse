# -*- coding: utf-8 -*-
# Copyright 2014 OpenMarket Ltd
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

from twisted.internet import defer

from synapse.api.events.room import (
    RoomMemberEvent, RoomTopicEvent, FeedbackEvent, RoomNameEvent,
    RoomRedactionEvent,
)

from synapse.util.logutils import log_function

from .directory import DirectoryStore
from .feedback import FeedbackStore
from .presence import PresenceStore
from .profile import ProfileStore
from .registration import RegistrationStore
from .room import RoomStore
from .roommember import RoomMemberStore
from .stream import StreamStore
from .transactions import TransactionStore
from .keys import KeyStore
from .event_federation import EventFederationStore

from .state import StateStore
from .signatures import SignatureStore

from syutil.base64util import decode_base64

from synapse.crypto.event_signing import compute_event_reference_hash


import json
import logging
import os


logger = logging.getLogger(__name__)


SCHEMAS = [
    "transactions",
    "users",
    "profiles",
    "presence",
    "im",
    "room_aliases",
    "keys",
    "redactions",
    "state",
    "event_edges",
    "event_signatures",
]


# Remember to update this number every time an incompatible change is made to
# database schema files, so the users will be informed on server restarts.
SCHEMA_VERSION = 7


class _RollbackButIsFineException(Exception):
    """ This exception is used to rollback a transaction without implying
    something went wrong.
    """
    pass


class DataStore(RoomMemberStore, RoomStore,
                RegistrationStore, StreamStore, ProfileStore, FeedbackStore,
                PresenceStore, TransactionStore,
                DirectoryStore, KeyStore, StateStore, SignatureStore,
                EventFederationStore, ):

    def __init__(self, hs):
        super(DataStore, self).__init__(hs)
        self.event_factory = hs.get_event_factory()
        self.hs = hs

        self.min_token_deferred = self._get_min_token()
        self.min_token = None

    @defer.inlineCallbacks
    @log_function
    def persist_event(self, event, backfilled=False, is_new_state=True,
                      current_state=None):
        stream_ordering = None
        if backfilled:
            if not self.min_token_deferred.called:
                yield self.min_token_deferred
            self.min_token -= 1
            stream_ordering = self.min_token

        try:
            yield self.runInteraction(
                "persist_event",
                self._persist_event_txn,
                event=event,
                backfilled=backfilled,
                stream_ordering=stream_ordering,
                is_new_state=is_new_state,
                current_state=current_state,
            )
        except _RollbackButIsFineException:
            pass

    @defer.inlineCallbacks
    def get_event(self, event_id, allow_none=False):
        events_dict = yield self._simple_select_one(
            "events",
            {"event_id": event_id},
            [
                "event_id",
                "type",
                "room_id",
                "content",
                "unrecognized_keys",
                "depth",
            ],
            allow_none=allow_none,
        )

        if not events_dict:
            defer.returnValue(None)

        event = yield self._parse_events([events_dict])
        defer.returnValue(event[0])

    @log_function
    def _persist_event_txn(self, txn, event, backfilled, stream_ordering=None,
                           is_new_state=True, current_state=None):
        if event.type == RoomMemberEvent.TYPE:
            self._store_room_member_txn(txn, event)
        elif event.type == FeedbackEvent.TYPE:
            self._store_feedback_txn(txn, event)
        elif event.type == RoomNameEvent.TYPE:
            self._store_room_name_txn(txn, event)
        elif event.type == RoomTopicEvent.TYPE:
            self._store_room_topic_txn(txn, event)
        elif event.type == RoomRedactionEvent.TYPE:
            self._store_redaction(txn, event)

        outlier = False
        if hasattr(event, "outlier"):
            outlier = event.outlier

        vals = {
            "topological_ordering": event.depth,
            "event_id": event.event_id,
            "type": event.type,
            "room_id": event.room_id,
            "content": json.dumps(event.content),
            "processed": True,
            "outlier": outlier,
            "depth": event.depth,
        }

        if stream_ordering is not None:
            vals["stream_ordering"] = stream_ordering

        unrec = {
            k: v
            for k, v in event.get_full_dict().items()
            if k not in vals.keys() and k not in [
                "redacted",
                "redacted_because",
                "signatures",
                "hashes",
                "prev_events",
            ]
        }
        vals["unrecognized_keys"] = json.dumps(unrec)

        try:
            self._simple_insert_txn(
                txn,
                "events",
                vals,
                or_replace=(not outlier),
                or_ignore=bool(outlier),
            )
        except:
            logger.warn(
                "Failed to persist, probably duplicate: %s",
                event.event_id,
                exc_info=True,
            )
            raise _RollbackButIsFineException("_persist_event")

        self._handle_prev_events(
            txn,
            outlier=outlier,
            event_id=event.event_id,
            prev_events=event.prev_events,
            room_id=event.room_id,
        )

        self._store_state_groups_txn(txn, event)

        if current_state:
            txn.execute("DELETE FROM current_state_events")

            for s in current_state:
                self._simple_insert_txn(
                    txn,
                    "current_state_events",
                    {
                        "event_id": s.event_id,
                        "room_id": s.room_id,
                        "type": s.type,
                        "state_key": s.state_key,
                    },
                    or_replace=True,
                )

        is_state = hasattr(event, "state_key") and event.state_key is not None
        if is_state:
            vals = {
                "event_id": event.event_id,
                "room_id": event.room_id,
                "type": event.type,
                "state_key": event.state_key,
            }

            if hasattr(event, "replaces_state"):
                vals["prev_state"] = event.replaces_state

            self._simple_insert_txn(
                txn,
                "state_events",
                vals,
                or_replace=True,
            )

            if is_new_state:
                self._simple_insert_txn(
                    txn,
                    "current_state_events",
                    {
                        "event_id": event.event_id,
                        "room_id": event.room_id,
                        "type": event.type,
                        "state_key": event.state_key,
                    },
                    or_replace=True,
                )

            for e_id, h in event.prev_state:
                self._simple_insert_txn(
                    txn,
                    table="event_edges",
                    values={
                        "event_id": event.event_id,
                        "prev_event_id": e_id,
                        "room_id": event.room_id,
                        "is_state": 1,
                    },
                    or_ignore=True,
                )

            if not backfilled:
                self._simple_insert_txn(
                    txn,
                    table="state_forward_extremities",
                    values={
                        "event_id": event.event_id,
                        "room_id": event.room_id,
                        "type": event.type,
                        "state_key": event.state_key,
                    },
                    or_replace=True,
                )

                for prev_state_id, _ in event.prev_state:
                    self._simple_delete_txn(
                        txn,
                        table="state_forward_extremities",
                        keyvalues={
                            "event_id": prev_state_id,
                        }
                    )

        for hash_alg, hash_base64 in event.hashes.items():
            hash_bytes = decode_base64(hash_base64)
            self._store_event_content_hash_txn(
                txn, event.event_id, hash_alg, hash_bytes,
            )

        if hasattr(event, "signatures"):
            logger.debug("sigs: %s", event.signatures)
            for name, sigs in event.signatures.items():
                for key_id, signature_base64 in sigs.items():
                    signature_bytes = decode_base64(signature_base64)
                    self._store_event_signature_txn(
                        txn, event.event_id, name, key_id,
                        signature_bytes,
                    )

        for prev_event_id, prev_hashes in event.prev_events:
            for alg, hash_base64 in prev_hashes.items():
                hash_bytes = decode_base64(hash_base64)
                self._store_prev_event_hash_txn(
                    txn, event.event_id, prev_event_id, alg, hash_bytes
                )

        for auth_id, _ in event.auth_events:
            self._simple_insert_txn(
                txn,
                table="event_auth",
                values={
                    "event_id": event.event_id,
                    "room_id": event.room_id,
                    "auth_id": auth_id,
                },
                or_ignore=True,
            )

        (ref_alg, ref_hash_bytes) = compute_event_reference_hash(event)
        self._store_event_reference_hash_txn(
            txn, event.event_id, ref_alg, ref_hash_bytes
        )

        if not outlier:
            self._update_min_depth_for_room_txn(
                txn,
                event.room_id,
                event.depth
            )

    def _store_redaction(self, txn, event):
        txn.execute(
            "INSERT OR IGNORE INTO redactions "
            "(event_id, redacts) VALUES (?,?)",
            (event.event_id, event.redacts)
        )

    @defer.inlineCallbacks
    def get_current_state(self, room_id, event_type=None, state_key=""):
        del_sql = (
            "SELECT event_id FROM redactions WHERE redacts = e.event_id "
            "LIMIT 1"
        )

        sql = (
            "SELECT e.*, (%(redacted)s) AS redacted FROM events as e "
            "INNER JOIN current_state_events as c ON e.event_id = c.event_id "
            "INNER JOIN state_events as s ON e.event_id = s.event_id "
            "WHERE c.room_id = ? "
        ) % {
            "redacted": del_sql,
        }

        if event_type:
            sql += " AND s.type = ? AND s.state_key = ? "
            args = (room_id, event_type, state_key)
        else:
            args = (room_id, )

        results = yield self._execute_and_decode(sql, *args)

        events = yield self._parse_events(results)
        defer.returnValue(events)

    @defer.inlineCallbacks
    def _get_min_token(self):
        row = yield self._execute(
            None,
            "SELECT MIN(stream_ordering) FROM events"
        )

        self.min_token = row[0][0] if row and row[0] and row[0][0] else -1
        self.min_token = min(self.min_token, -1)

        logger.debug("min_token is: %s", self.min_token)

        defer.returnValue(self.min_token)

    def insert_client_ip(self, user, access_token, device_id, ip, user_agent):
        return self._simple_insert(
            "user_ips",
            {
                "user": user.to_string(),
                "access_token": access_token,
                "device_id": device_id,
                "ip": ip,
                "user_agent": user_agent,
                "last_seen": int(self._clock.time_msec()),
            }
        )

    def get_user_ip_and_agents(self, user):
        return self._simple_select_list(
            table="user_ips",
            keyvalues={"user": user.to_string()},
            retcols=[
                "device_id", "access_token", "ip", "user_agent", "last_seen"
            ],
        )

    def snapshot_room(self, event):
        """Snapshot the room for an update by a user
        Args:
            room_id (synapse.types.RoomId): The room to snapshot.
            user_id (synapse.types.UserId): The user to snapshot the room for.
            state_type (str): Optional state type to snapshot.
            state_key (str): Optional state key to snapshot.
        Returns:
            synapse.storage.Snapshot: A snapshot of the state of the room.
        """
        def _snapshot(txn):
            prev_events = self._get_latest_events_in_room(
                txn,
                event.room_id
            )

            prev_state = None
            state_key = None
            if hasattr(event, "state_key"):
                state_key = event.state_key
                prev_state = self._get_latest_state_in_room(
                    txn,
                    event.room_id,
                    type=event.type,
                    state_key=state_key,
                )

            return Snapshot(
                store=self,
                room_id=event.room_id,
                user_id=event.user_id,
                prev_events=prev_events,
                prev_state=prev_state,
                state_type=event.type,
                state_key=state_key,
            )

        return self.runInteraction("snapshot_room", _snapshot)


class Snapshot(object):
    """Snapshot of the state of a room
    Args:
        store (DataStore): The datastore.
        room_id (RoomId): The room of the snapshot.
        user_id (UserId): The user this snapshot is for.
        prev_events (list): The list of event ids this snapshot is after.
        membership_state (RoomMemberEvent): The current state of the user in
            the room.
        state_type (str, optional): State type captured by the snapshot
        state_key (str, optional): State key captured by the snapshot
        prev_state_pdu (PduEntry, optional): pdu id of
            the previous value of the state type and key in the room.
    """

    def __init__(self, store, room_id, user_id, prev_events,
                 prev_state, state_type=None, state_key=None):
        self.store = store
        self.room_id = room_id
        self.user_id = user_id
        self.prev_events = prev_events
        self.prev_state = prev_state
        self.state_type = state_type
        self.state_key = state_key

    def fill_out_prev_events(self, event):
        if not hasattr(event, "prev_events"):
            event.prev_events = [
                (event_id, hashes)
                for event_id, hashes, _ in self.prev_events
            ]

            if self.prev_events:
                event.depth = max([int(v) for _, _, v in self.prev_events]) + 1
            else:
                event.depth = 0

        if not hasattr(event, "prev_state") and self.prev_state is not None:
            event.prev_state = self.prev_state


def schema_path(schema):
    """ Get a filesystem path for the named database schema

    Args:
        schema: Name of the database schema.
    Returns:
        A filesystem path pointing at a ".sql" file.

    """
    dir_path = os.path.dirname(__file__)
    schemaPath = os.path.join(dir_path, "schema", schema + ".sql")
    return schemaPath


def read_schema(schema):
    """ Read the named database schema.

    Args:
        schema: Name of the datbase schema.
    Returns:
        A string containing the database schema.
    """
    with open(schema_path(schema)) as schema_file:
        return schema_file.read()


def prepare_database(db_conn):
    """ Set up all the dbs. Since all the *.sql have IF NOT EXISTS, so we
    don't have to worry about overwriting existing content.
    """
    c = db_conn.cursor()
    c.execute("PRAGMA user_version")
    row = c.fetchone()

    if row and row[0]:
        user_version = row[0]

        if user_version > SCHEMA_VERSION:
            raise ValueError(
                "Cannot use this database as it is too " +
                "new for the server to understand"
            )
        elif user_version < SCHEMA_VERSION:
            logger.info(
                "Upgrading database from version %d",
                user_version
            )

            # Run every version since after the current version.
            for v in range(user_version + 1, SCHEMA_VERSION + 1):
                sql_script = read_schema("delta/v%d" % (v))
                c.executescript(sql_script)

            db_conn.commit()

    else:
        sql_script = "BEGIN TRANSACTION;\n"
        for sql_loc in SCHEMAS:
            sql_script += read_schema(sql_loc)
            sql_script += "\n"
        sql_script += "COMMIT TRANSACTION;"
        c.executescript(sql_script)
        db_conn.commit()
        c.execute("PRAGMA user_version = %d" % SCHEMA_VERSION)

    c.close()
