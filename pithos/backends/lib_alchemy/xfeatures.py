# Copyright 2011 GRNET S.A. All rights reserved.
# 
# Redistribution and use in source and binary forms, with or
# without modification, are permitted provided that the following
# conditions are met:
# 
#   1. Redistributions of source code must retain the above
#      copyright notice, this list of conditions and the following
#      disclaimer.
# 
#   2. Redistributions in binary form must reproduce the above
#      copyright notice, this list of conditions and the following
#      disclaimer in the documentation and/or other materials
#      provided with the distribution.
# 
# THIS SOFTWARE IS PROVIDED BY GRNET S.A. ``AS IS'' AND ANY EXPRESS
# OR IMPLIED WARRANTIES, INCLUDING, BUT NOT LIMITED TO, THE IMPLIED
# WARRANTIES OF MERCHANTABILITY AND FITNESS FOR A PARTICULAR
# PURPOSE ARE DISCLAIMED. IN NO EVENT SHALL GRNET S.A OR
# CONTRIBUTORS BE LIABLE FOR ANY DIRECT, INDIRECT, INCIDENTAL,
# SPECIAL, EXEMPLARY, OR CONSEQUENTIAL DAMAGES (INCLUDING, BUT NOT
# LIMITED TO, PROCUREMENT OF SUBSTITUTE GOODS OR SERVICES; LOSS OF
# USE, DATA, OR PROFITS; OR BUSINESS INTERRUPTION) HOWEVER CAUSED
# AND ON ANY THEORY OF LIABILITY, WHETHER IN CONTRACT, STRICT
# LIABILITY, OR TORT (INCLUDING NEGLIGENCE OR OTHERWISE) ARISING IN
# ANY WAY OUT OF THE USE OF THIS SOFTWARE, EVEN IF ADVISED OF THE
# POSSIBILITY OF SUCH DAMAGE.
# 
# The views and conclusions contained in the software and
# documentation are those of the authors and should not be
# interpreted as representing official policies, either expressed
# or implied, of GRNET S.A.

from collections import defaultdict
from sqlalchemy import Table, Column, String, Integer, MetaData, ForeignKey
from sqlalchemy.sql import select, and_
from sqlalchemy.schema import Index
from sqlalchemy.sql.expression import desc

from dbworker import DBWorker


class XFeatures(DBWorker):
    """XFeatures are path properties that allow non-nested
       inheritance patterns. Currently used for storing permissions.
    """
    
    def __init__(self, **params):
        DBWorker.__init__(self, **params)
        metadata = MetaData()
        columns=[]
        columns.append(Column('feature_id', Integer, primary_key=True))
        columns.append(Column('path', String(2048)))
        self.xfeatures = Table('xfeatures', metadata, *columns)
        # place an index on path
        Index('idx_features_path', self.xfeatures.c.path)
        
        columns=[]
        columns.append(Column('feature_id', Integer,
                              ForeignKey('xfeatures.feature_id',
                                         ondelete='CASCADE'),
                              primary_key=True))
        columns.append(Column('key', Integer, primary_key=True,
                              autoincrement=False))
        columns.append(Column('value', String(255), primary_key=True))
        self.xfeaturevals = Table('xfeaturevals', metadata, *columns)
        
        metadata.create_all(self.engine)
    
    def xfeature_inherit(self, path):
        """Return the (path, feature) inherited by the path, or None."""
        
        s = select([self.xfeatures.c.path, self.xfeatures.c.feature_id])
        s = s.where(self.xfeatures.c.path <= path)
        s = s.order_by(desc(self.xfeatures.c.path)).limit(1)
        r = self.conn.execute(s)
        row = r.fetchone()
        r.close()
        if row and path.startswith(row[0]):
            return row
        else:
            return None
    
    def xfeature_list(self, path):
        """Return the list of the (prefix, feature) pairs matching path.
           A prefix matches path if either the prefix includes the path,
           or the path includes the prefix.
        """
        
        inherited = self.xfeature_inherit(path)
        if inherited:
            return [inherited]
        
        s = select([self.xfeatures.c.path, self.xfeatures.c.feature_id])
        s = s.where(and_(self.xfeatures.c.path.like(path + '%'),
                     self.xfeatures.c.path != path))
        s = s.order_by(self.xfeatures.c.path)
        r = self.conn.execute(s)
        l = r.fetchall()
        r.close()
        return l
    
    def xfeature_create(self, path):
        """Create and return a feature for path.
           If the path already inherits a feature or
           bestows to paths already inheriting a feature,
           create no feature and return None.
           If the path has a feature, return it.
        """
        
        prefixes = self.xfeature_list(path)
        pl = len(prefixes)
        if (pl > 1) or (pl == 1 and prefixes[0][0] != path):
            return None
        if pl == 1 and prefixes[0][0] == path:
            return prefixes[0][1]
        s = self.xfeatures.insert()
        r = self.conn.execute(s, path=path)
        inserted_primary_key = r.inserted_primary_key[0]
        r.close()
        return inserted_primary_key
    
    def xfeature_destroy(self, path):
        """Destroy a feature and all its key, value pairs."""
        
        s = self.xfeatures.delete().where(self.xfeatures.c.path == path)
        r = self.conn.execute(s)
        r.close()
    
    def feature_dict(self, feature):
        """Return a dict mapping keys to list of values for feature."""
        
        s = select([self.xfeaturevals.c.key, self.xfeaturevals.c.value])
        s = s.where(self.xfeaturevals.c.feature_id == feature)
        r = self.conn.execute(s)
        d = defaultdict(list)
        for key, value in r.fetchall():
            d[key].append(value)
        r.close()
        return d
    
    def feature_set(self, feature, key, value):
        """Associate a key, value pair with a feature."""
        
        s = self.xfeaturevals.insert()
        r = self.conn.execute(s, feature_id=feature, key=key, value=value)
        r.close()
    
    def feature_setmany(self, feature, key, values):
        """Associate the given key, and values with a feature."""
        
        s = self.xfeaturevals.insert()
        values = [{'feature_id':feature, 'key':key, 'value':v} for v in values]
        r = self.conn.execute(s, values)
        r.close()
    
    def feature_unset(self, feature, key, value):
        """Disassociate a key, value pair from a feature."""
        
        s = self.xfeaturevals.delete()
        s = s.where(and_(self.xfeaturevals.c.feature_id == feature,
                     self.xfeaturevals.c.key == key,
                     self.xfeaturevals.c.value == value))
        r = self.conn.execute(s)
        r.close()
    
    def feature_unsetmany(self, feature, key, values):
        """Disassociate the key for the values given, from a feature."""
        
        for v in values:
            conditional = and_(self.xfeaturevals.c.feature_id == feature,
                               self.xfeaturevals.c.key == key,
                               self.xfeaturevals.c.value == v)
            s = self.xfeaturevals.delete().where(conditional)
            r = self.conn.execute(s)
            r.close()
        
    def feature_get(self, feature, key):
        """Return the list of values for a key of a feature."""
        
        s = select([self.xfeaturevals.c.value])
        s = s.where(and_(self.xfeaturevals.c.feature_id == feature,
                     self.xfeaturevals.c.key == key))
        r = self.conn.execute(s)
        l = [row[0] for row in r.fetchall()]
        r.close()
        return l
    
    def feature_clear(self, feature, key):
        """Delete all key, value pairs for a key of a feature."""
        
        s = self.xfeaturevals.delete()
        s = s.where(and_(self.xfeaturevals.c.feature_id == feature,
                     self.xfeaturevals.c.key == key))
        r = self.conn.execute(s)
        r.close()
