import time
import pymongo
import logging

from oplogwatcher import OplogWatcher

class OplogReplayer(OplogWatcher):
    """ Replayes all oplogs from one mongo connection to another.

    Watches a mongo connection for write ops (source), and replays them
    into another mongo connection (destination).
    """

    @staticmethod
    def is_create_index(raw):
        """ Determines if the given operation is a "create index"" operation.

        { "op" : "i",
          "ns" : "testdb.system.indexes" }
        """
        return raw['op'] == 'i' and raw['ns'].endswith('.system.indexes')

    @staticmethod
    def is_drop_index(raw):
        """ Determines if the given operation is a "drop index" operation.

        { "op" : "c",
          "ns" : "testdb.$cmd",
          "o" : { "dropIndexes" : "testcoll",
        		  "index" : "nuie_1" } }
        """
        return raw['op'] == 'c' and 'dropIndexes' in raw['o']

    @staticmethod
    def is_index_operation(raw):
        return (OplogReplayer.is_create_index(raw) or
                OplogReplayer.is_drop_index(raw))

    def __init__(self, source, dest, replay_indexes=True, poll_time=1.0):
        # Create a one-time connection to source, to determine replicaset.
        c = pymongo.Connection(source)
        try:
            obj = c.local.system.replset.find_one()
            replicaset = obj['_id']
        except:
            raise ValueError('Could not determine replicaset for %r' % source)

        # Mongo source is a replica set, connect to it as such.
        self.source = pymongo.Connection(source, replicaset=replicaset)
        # Use ReadPreference.SECONDARY because we can afford to read oplogs
        # from secondaries: even if they're behind, everything will work
        # correctly because the oplog order will always be preserved.
        self.source.read_preference = pymongo.ReadPreference.SECONDARY

        self._lastts_id = '%s-lastts' % replicaset
        self.dest = pymongo.Connection(dest)

        self.replay_indexes = replay_indexes

        ts = self._get_lastts()
        self._replay_count = 0
        OplogWatcher.__init__(self, self.source, ts=ts, poll_time=poll_time)

    def print_replication_info(self):
        delay = int(time.time()) - self.ts.time
        logging.debug('synced = %ssecs ago (%.2fhrs)' % (delay, delay/3600.0))
        logging.debug('[%s] replayed %s ops' % (time.strftime('%x %X'),
                      self._replay_count))

    def _get_lastts(self):
        # Get the last oplog ts that was played on destination.
        obj = self.dest.oplogreplay.settings.find_one({'_id': self._lastts_id})
        if obj is None:
            return None
        else:
            return obj['value']

    def _update_lastts(self):
        self.dest.oplogreplay.settings.update({'_id': self._lastts_id},
                                              {'$set': {'value': self.ts}},
                                              upsert=True)

    def process_op(self, ns, raw):
        if not self.replay_indexes and OplogReplayer.is_index_operation(raw):
            # Do not replay index operations.
            pass
        else:
            # Treat "drop index" operations separately.
            if OplogReplayer.is_drop_index(raw):
                self.drop_index(raw)
            else:
                OplogWatcher.process_op(self, ns, raw)

        # Update the lastts on the destination
        self._update_lastts()
        self._replay_count += 1
        if self._replay_count % 100 == 0:
            self.print_replication_info()

    def _dest_coll(self, ns):
        db, collection = ns.split('.', 1)
        return self.dest[db][collection]

    def insert(self, ns, docid, raw, **kw):
        """ Perform a single insert operation.

            {'docid': ObjectId('4e95ae77a20e6164850761cd'),
             'ns': u'mydb.tweets',
             'raw': {u'h': -1469300750073380169L,
                     u'ns': u'mydb.tweets',
                     u'o': {u'_id': ObjectId('4e95ae77a20e6164850761cd'),
                            u'content': u'Lorem ipsum',
                            u'nr': 16},
                     u'op': u'i',
                     u'ts': Timestamp(1318432375, 1)}}
        """
        self._dest_coll(ns).insert(raw['o'], safe=True)

    def update(self, ns, docid, raw, **kw):
        """ Perform a single update operation.

            {'docid': ObjectId('4e95ae3616692111bb000001'),
             'ns': u'mydb.tweets',
             'raw': {u'h': -5295451122737468990L,
                     u'ns': u'mydb.tweets',
                     u'o': {u'$set': {u'content': u'Lorem ipsum'}},
                     u'o2': {u'_id': ObjectId('4e95ae3616692111bb000001')},
                     u'op': u'u',
                     u'ts': Timestamp(1318432339, 1)}}
        """
        self._dest_coll(ns).update(raw['o2'], raw['o'], safe=True)

    def delete(self, ns, docid, raw, **kw):
        """ Perform a single delete operation.

            {'docid': ObjectId('4e959ea11669210edc002902'),
             'ns': u'mydb.tweets',
             'raw': {u'b': True,
                     u'h': -8347418295715732480L,
                     u'ns': u'mydb.tweets',
                     u'o': {u'_id': ObjectId('4e959ea11669210edc002902')},
                     u'op': u'd',
                     u'ts': Timestamp(1318432261, 10499)}}
        """
        self._dest_coll(ns).remove(raw['o'], safe=True)

    def drop_index(self, raw):
        """ Executes a drop index command.

            { "op" : "c",
              "ns" : "testdb.$cmd",
              "o" : { "dropIndexes" : "testcoll",
            		  "index" : "nuie_1" } }
        """
        dbname = raw['ns'].split('.', 1)[0]
        collname = raw['o']['dropIndexes']
        self.dest[dbname][collname].drop_index(raw['o']['index'])
