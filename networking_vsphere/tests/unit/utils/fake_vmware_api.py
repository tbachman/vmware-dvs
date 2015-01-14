# Copyright (c) 2014 Hewlett-Packard Development Company, L.P.
# All Rights Reserved.
#
#    Licensed under the Apache License, Version 2.0 (the "License"); you may
#    not use this file except in compliance with the License. You may obtain
#    a copy of the License at
#
#         http://www.apache.org/licenses/LICENSE-2.0
#
#    Unless required by applicable law or agreed to in writing, software
#    distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
#    WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
#    License for the specific language governing permissions and limitations
#    under the License.


class DataObject(object):

    """Data object base class."""
    pass


class FakeFactory(object):
    """Fake factory class with create method."""

    def create(self, obj_name):
        return DataObject()


class FakeDynamicPropObject(object):
    """Fake Dynamic Property object."""

    def __init__(self):
        self.name = "name"
        self.val = "val"


class FakeObjectContent(object):
    """Fake ObjectContent object."""

    def __init__(self):
        self.propSet = [FakeDynamicPropObject()]


class FakeRetrieveResultObject(object):
    """Fake RetrieveResult object."""

    def __init__(self):
        self.objects = [FakeObjectContent()]
        self.token = "1234"


class FakeVim(object):
    """Fake Vim object class."""

    def __init__(self):
        self.client = DataObject()
        self.client.factory = FakeFactory()
        service_content = self.client.factory.create('ns0:ServiceContent')
        service_content.propertyCollector = "PropCollector"
        self.service_content = service_content
        self.RetrievePropertiesExCalled = False
        self.ContinueRetrievePropertiesExCalled = False

    def RetrievePropertiesEx(self, prop_coll, specSet, options):
        self.RetrievePropertiesExCalled = True
        res = FakeRetrieveResultObject()
        return res

    def ContinueRetrievePropertiesEx(self, prop_coll, token=None):
        self.ContinueRetrievePropertiesExCalled = True
        return
