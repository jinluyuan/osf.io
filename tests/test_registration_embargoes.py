"""Tests related to embargoes of registrations"""
import datetime
import json

import mock
from nose.tools import *  #noqa
from tests.base import fake, OsfTestCase
from tests.factories import (
    AuthUserFactory, EmbargoFactory, NodeFactory, ProjectFactory,
    RegistrationFactory, UserFactory, UnconfirmedUserFactory
)

from framework.exceptions import PermissionsError
from modularodm.exceptions import ValidationValueError
from website.exceptions import (
    InvalidEmbargoDisapprovalToken, InvalidEmbargoApprovalToken, NodeStateError,
)
from website.models import Embargo, Node
from website.project.model import ensure_schemas


class RegistrationEmbargoModelsTestCase(OsfTestCase):
    def setUp(self):
        super(RegistrationEmbargoModelsTestCase, self).setUp()
        self.user = UserFactory()
        self.project = ProjectFactory(creator=self.user)
        self.registration = RegistrationFactory(project=self.project)
        self.embargo = EmbargoFactory(user=self.user)
        self.valid_embargo_end_date = datetime.datetime.utcnow() + datetime.timedelta(days=3)

    # Validator tests
    def test_invalid_state_raises_ValidationValueError(self):
        with assert_raises(ValidationValueError):
            self.embargo.state = 'not a valid state'
            self.embargo.save()

    # Node#_initiate_embargo tests
    def test__initiate_embargo_does_not_save_embargo(self):
        initial_count = Embargo.find().count()
        self.registration._initiate_embargo(
            self.user,
            self.valid_embargo_end_date,
            for_existing_registration=True
        )
        self.assertEqual(Embargo.find().count(), initial_count)

    def test__initiate_embargo_does_not_create_tokens_for_unregistered_admin(self):
        unconfirmed_user = UnconfirmedUserFactory()
        self.registration.contributors.append(unconfirmed_user)
        self.registration.add_permission(unconfirmed_user, 'admin', save=True)
        assert_true(self.registration.has_permission(unconfirmed_user, 'admin'))

        embargo = self.registration._initiate_embargo(
            self.user,
            self.valid_embargo_end_date,
            for_existing_registration=True
        )
        assert_true(self.user._id in embargo.approval_state)
        assert_false(unconfirmed_user._id in embargo.approval_state)

    def test__initiate_embargo_with_save_does_save_embargo(self):
        initial_count = Embargo.find().count()
        self.registration._initiate_embargo(
            self.user,
            self.valid_embargo_end_date,
            for_existing_registration=True,
            save=True
        )
        self.assertEqual(Embargo.find().count(), initial_count + 1)

    # Backref tests
    def test_embargo_initiator_has_backref(self):
        self.registration.embargo_registration(
            self.user,
            self.valid_embargo_end_date
        )
        self.registration.save()
        self.registration.reload()
        assert_equal(len(self.user.embargo__embargoed), 1)

    # Node#embargo_registration tests
    def test_embargo_from_non_admin_raises_PermissionsError(self):
        self.registration.remove_permission(self.user, 'admin')
        self.registration.save()
        self.registration.reload()
        with assert_raises(PermissionsError):
            self.registration.embargo_registration(self.user, self.valid_embargo_end_date)

    def test_embargo_end_date_in_past_raises_ValidationValueError(self):
        with assert_raises(ValidationValueError):
            self.registration.embargo_registration(
                self.user,
                datetime.datetime(1999, 1, 1)
            )

    def test_embargo_end_date_today_raises_ValidationValueError(self):
        with assert_raises(ValidationValueError):
            self.registration.embargo_registration(
                self.user,
                datetime.datetime.utcnow()
            )

    def test_embargo_end_date_in_far_future_raises_ValidationValueError(self):
        with assert_raises(ValidationValueError):
            self.registration.embargo_registration(
                self.user,
                datetime.datetime(2099, 1, 1)
            )

    def test_embargo_with_valid_end_date_starts_pending_embargo(self):
        self.registration.embargo_registration(
            self.user,
            datetime.datetime.utcnow() + datetime.timedelta(days=10)
        )
        self.registration.save()
        assert_true(self.registration.pending_embargo)

    def test_embargo_public_project_makes_private_pending_embargo(self):
        self.registration.is_public = True
        assert_true(self.registration.is_public)
        self.registration.embargo_registration(
            self.user,
            datetime.datetime.utcnow() + datetime.timedelta(days=10)
        )
        self.registration.save()
        assert_true(self.registration.pending_embargo)
        assert_false(self.registration.is_public)

    def test_embargo_non_registration_raises_NodeStateError(self):
        self.registration.is_registration = False
        self.registration.save()
        with assert_raises(NodeStateError):
            self.registration.embargo_registration(
                self.user,
                datetime.datetime.utcnow() + datetime.timedelta(days=10)
            )
        assert_false(self.registration.pending_embargo)

    # Embargo#approve_embargo tests
    def test_invalid_approval_token_raises_InvalidEmbargoApprovalToken(self):
        self.registration.embargo_registration(
            self.user,
            datetime.datetime.utcnow() + datetime.timedelta(days=10)
        )
        self.registration.save()
        assert_true(self.registration.pending_embargo)

        invalid_approval_token = 'not a real token'
        with assert_raises(InvalidEmbargoApprovalToken):
            self.registration.embargo.approve_embargo(self.user, invalid_approval_token)
        assert_true(self.registration.pending_embargo)
        assert_false(self.registration.embargo_end_date)

    def test_non_admin_approval_token_raises_PermissionsError(self):
        non_admin = UserFactory()
        self.registration.embargo_registration(
            self.user,
            datetime.datetime.utcnow() + datetime.timedelta(days=10)
        )
        self.registration.save()
        assert_true(self.registration.pending_embargo)

        approval_token = self.registration.embargo.approval_state[self.user._id]['approval_token']
        with assert_raises(PermissionsError):
            self.registration.embargo.approve_embargo(non_admin, approval_token)
        assert_true(self.registration.pending_embargo)

    def test_one_approval_with_one_admin_embargoes(self):
        self.registration.embargo_registration(
            self.user,
            datetime.datetime.utcnow() + datetime.timedelta(days=10)
        )
        self.registration.save()
        assert_true(self.registration.pending_embargo)

        approval_token = self.registration.embargo.approval_state[self.user._id]['approval_token']
        self.registration.embargo.approve_embargo(self.user, approval_token)
        assert_true(self.registration.embargo_end_date)
        assert_false(self.registration.pending_embargo)

    def test_approval_adds_to_parent_projects_log(self):
        initial_project_logs = len(self.registration.registered_from.logs)
        self.registration.embargo_registration(
            self.user,
            datetime.datetime.utcnow() + datetime.timedelta(days=10)
        )
        self.registration.save()

        approval_token = self.registration.embargo.approval_state[self.user._id]['approval_token']
        self.registration.embargo.approve_embargo(self.user, approval_token)
        # Logs: Created, registered, embargo initiated, embargo approved
        assert_equal(len(self.registration.registered_from.logs), initial_project_logs + 2)

    def test_one_approval_with_two_admins_stays_pending(self):
        admin2 = UserFactory()
        self.registration.contributors.append(admin2)
        self.registration.add_permission(admin2, 'admin', save=True)
        self.registration.embargo_registration(
            self.user,
            datetime.datetime.utcnow() + datetime.timedelta(days=10)
        )
        self.registration.save()

        # First admin approves
        approval_token = self.registration.embargo.approval_state[self.user._id]['approval_token']
        self.registration.embargo.approve_embargo(self.user, approval_token)
        assert_true(self.registration.pending_embargo)
        num_of_approvals = sum([val['has_approved'] for val in self.registration.embargo.approval_state.values()])
        assert_equal(num_of_approvals, 1)

        # Second admin approves
        approval_token = self.registration.embargo.approval_state[admin2._id]['approval_token']
        self.registration.embargo.approve_embargo(admin2, approval_token)
        assert_true(self.registration.embargo_end_date)
        assert_false(self.registration.pending_embargo)
        num_of_approvals = sum([val['has_approved'] for val in self.registration.embargo.approval_state.values()])
        assert_equal(num_of_approvals, 2)

    # Embargo#disapprove_embargo tests
    def test_invalid_disapproval_token_raises_InvalidEmbargoDisapprovalToken(self):
        self.registration.embargo_registration(
            self.user,
            datetime.datetime.utcnow() + datetime.timedelta(days=10)
        )
        self.registration.save()
        assert_true(self.registration.pending_embargo)
        with assert_raises(InvalidEmbargoDisapprovalToken):
            self.registration.embargo.disapprove_embargo(self.user, fake.sentence())
        assert_true(self.registration.pending_embargo)
        assert_false(self.registration.embargo_end_date)

    def test_non_admin_disapproval_token_raises_PermissionsError(self):
        non_admin = UserFactory()
        self.registration.embargo_registration(
            self.user,
            datetime.datetime.utcnow() + datetime.timedelta(days=10)
        )
        self.registration.save()
        assert_true(self.registration.pending_embargo)

        disapproval_token = self.registration.embargo.approval_state[self.user._id]['disapproval_token']
        with assert_raises(PermissionsError):
            self.registration.embargo.disapprove_embargo(non_admin, disapproval_token)
        assert_true(self.registration.pending_embargo)

    def test_one_disapproval_cancels_embargo(self):
        self.registration.embargo_registration(
            self.user,
            datetime.datetime.utcnow() + datetime.timedelta(days=10)
        )
        self.registration.save()
        assert_true(self.registration.pending_embargo)

        disapproval_token = self.registration.embargo.approval_state[self.user._id]['disapproval_token']
        self.registration.embargo.disapprove_embargo(self.user, disapproval_token)
        assert_equal(self.registration.embargo.state, Embargo.CANCELLED)
        assert_false(self.registration.pending_embargo)
        assert_false(self.registration.embargo_end_date)

    def test_disapproval_adds_to_parent_projects_log(self):
        initial_project_logs = len(self.registration.registered_from.logs)
        self.registration.embargo_registration(
            self.user,
            datetime.datetime.utcnow() + datetime.timedelta(days=10)
        )
        self.registration.save()

        disapproval_token = self.registration.embargo.approval_state[self.user._id]['disapproval_token']
        registered_from = self.registration.registered_from
        self.registration.embargo.disapprove_embargo(self.user, disapproval_token)
        # Logs: Created, registered, embargo initiated, embargo cancelled
        assert_equal(len(registered_from.logs), initial_project_logs + 2)

    def test_cancelling_embargo_deletes_parent_registration(self):
        self.registration.embargo_registration(
            self.user,
            datetime.datetime.utcnow() + datetime.timedelta(days=10)
        )
        self.registration.save()

        disapproval_token = self.registration.embargo.approval_state[self.user._id]['disapproval_token']
        self.registration.embargo.disapprove_embargo(self.user, disapproval_token)
        assert_equal(self.registration.embargo.state, Embargo.CANCELLED)
        assert_true(self.registration.is_deleted)

    def test_cancelling_embargo_for_existing_registration_does_not_delete_registration(self):
        self.registration.embargo_registration(
            self.user,
            datetime.datetime.utcnow() + datetime.timedelta(days=10),
            for_existing_registration=True
        )
        self.registration.save()

        disapproval_token = self.registration.embargo.approval_state[self.user._id]['disapproval_token']
        self.registration.embargo.disapprove_embargo(self.user, disapproval_token)
        assert_equal(self.registration.embargo.state, Embargo.CANCELLED)
        assert_false(self.registration.is_deleted)

    # Embargo property tests
    def test_new_registration_is_pending_registration(self):
        self.registration.embargo_registration(
            self.user,
            datetime.datetime.utcnow() + datetime.timedelta(days=10)
        )
        self.registration.save()
        assert_true(self.registration.pending_registration)

    def test_existing_registration_is_not_pending_registration(self):
        self.registration.embargo_registration(
            self.user,
            datetime.datetime.utcnow() + datetime.timedelta(days=10),
            for_existing_registration=True
        )
        self.registration.save()
        assert_false(self.registration.pending_registration)


class RegistrationWithChildNodesEmbargoModelTestCase(OsfTestCase):
    def setUp(self):
        super(RegistrationWithChildNodesEmbargoModelTestCase, self).setUp()
        self.user = AuthUserFactory()
        self.auth = self.user.auth
        self.valid_embargo_end_date = datetime.datetime.utcnow() + datetime.timedelta(days=3)
        self.project = ProjectFactory(title='Root', is_public=False, creator=self.user)
        self.component = NodeFactory(
            creator=self.user,
            parent=self.project,
            title='Component'
        )
        self.subproject = ProjectFactory(
            creator=self.user,
            parent=self.project,
            title='Subproject'
        )
        self.subproject_component = NodeFactory(
            creator=self.user,
            parent=self.subproject,
            title='Subcomponent'
        )
        self.registration = RegistrationFactory(project=self.project)
        # Reload the registration; else tests won't catch failures to save
        self.registration.reload()

    def test_approval_embargoes_descendant_nodes(self):
        # Initiate embargo for parent registration
        self.registration.embargo_registration(
            self.user,
            self.valid_embargo_end_date
        )
        self.registration.save()
        assert_true(self.registration.pending_embargo)

        # Ensure descendant nodes are pending embargo
        descendants = self.registration.get_descendants_recursive()
        for node in descendants:
            assert_true(node.pending_embargo)

        # Approve parent registration's embargo
        approval_token = self.registration.embargo.approval_state[self.user._id]['approval_token']
        self.registration.embargo.approve_embargo(self.user, approval_token)
        assert_true(self.registration.embargo.embargo_end_date)

        # Ensure descendant nodes are in embargo
        descendants = self.registration.get_descendants_recursive()
        for node in descendants:
            assert_true(node.embargo_end_date)

    def test_disapproval_cancels_embargo_on_descendant_nodes(self):
        # Initiate embargo on parent registration
        self.registration.embargo_registration(
            self.user,
            self.valid_embargo_end_date
        )
        self.registration.save()
        assert_true(self.registration.pending_embargo)

        # Ensure descendant nodes are pending embargo
        descendants = self.registration.get_descendants_recursive()
        for node in descendants:
            assert_true(node.pending_embargo)

        # Disapprove parent registration's embargo
        disapproval_token = self.registration.embargo.approval_state[self.user._id]['disapproval_token']
        self.registration.embargo.disapprove_embargo(self.user, disapproval_token)
        assert_false(self.registration.pending_embargo)
        assert_false(self.registration.embargo_end_date)
        assert_equal(self.registration.embargo.state, Embargo.CANCELLED)

        # Ensure descendant nodes' embargoes are cancelled
        descendants = self.registration.get_descendants_recursive()
        for node in descendants:
            assert_false(node.pending_embargo)
            assert_false(node.embargo_end_date)


class RegistrationEmbargoApprovalDisapprovalViewsTestCase(OsfTestCase):
    def setUp(self):
        super(RegistrationEmbargoApprovalDisapprovalViewsTestCase, self).setUp()
        self.user = AuthUserFactory()
        self.registration = RegistrationFactory(creator=self.user)

    # node_registration_embargo_approve tests
    def test_GET_from_unauthorized_user_raises_HTTPForbidden(self):
        unauthorized_user = AuthUserFactory()
        res = self.app.get(
            self.registration.web_url_for('node_registration_embargo_approve', token=fake.sentence()),
            auth=unauthorized_user.auth,
            expect_errors=True
        )
        assert_equal(res.status_code, 403)

    def test_GET_approve_registration_without_embargo_raises_HTTPBad_Request(self):
        assert_false(self.registration.pending_embargo)
        res = self.app.get(
            self.registration.web_url_for('node_registration_embargo_approve', token=fake.sentence()),
            auth=self.user.auth,
            expect_errors=True
        )
        assert_equal(res.status_code, 400)

    def test_GET_approve_with_invalid_token_returns_HTTPBad_Request(self):
        self.registration.embargo_registration(
            self.user,
            datetime.datetime.utcnow() + datetime.timedelta(days=10)
        )
        self.registration.save()
        assert_true(self.registration.pending_embargo)

        res = self.app.get(
            self.registration.web_url_for('node_registration_embargo_approve', token=fake.sentence()),
            auth=self.user.auth,
            expect_errors=True
        )
        assert_equal(res.status_code, 400)

    def test_GET_approve_with_wrong_token_returns_HTTPBad_Request(self):
        admin2 = UserFactory()
        self.registration.contributors.append(admin2)
        self.registration.add_permission(admin2, 'admin', save=True)
        self.registration.embargo_registration(
            self.user,
            datetime.datetime.utcnow() + datetime.timedelta(days=10)
        )
        self.registration.save()
        assert_true(self.registration.pending_embargo)

        wrong_approval_token = self.registration.embargo.approval_state[admin2._id]['approval_token']
        res = self.app.get(
            self.registration.web_url_for('node_registration_embargo_approve', token=wrong_approval_token),
            auth=self.user.auth,
            expect_errors=True
        )
        assert_equal(res.status_code, 400)

    def test_GET_approve_with_wrong_admins_token_returns_HTTPBad_Request(self):
        admin2 = UserFactory()
        self.registration.contributors.append(admin2)
        self.registration.add_permission(admin2, 'admin', save=True)
        self.registration.embargo_registration(
            self.user,
            datetime.datetime.utcnow() + datetime.timedelta(days=10)
        )
        self.registration.save()
        assert_true(self.registration.pending_embargo)

        wrong_approval_token = self.registration.embargo.approval_state[admin2._id]['approval_token']
        res = self.app.get(
            self.registration.web_url_for('node_registration_embargo_approve', token=wrong_approval_token),
            auth=self.user.auth,
            expect_errors=True
        )
        assert_true(self.registration.pending_embargo)
        assert_equal(res.status_code, 400)

    def test_GET_approve_with_valid_token_returns_redirect(self):
        self.registration.embargo_registration(
            self.user,
            datetime.datetime.utcnow() + datetime.timedelta(days=10)
        )
        self.registration.save()
        assert_true(self.registration.pending_embargo)

        approval_token = self.registration.embargo.approval_state[self.user._id]['approval_token']
        res = self.app.get(
            self.registration.web_url_for('node_registration_embargo_approve', token=approval_token),
            auth=self.user.auth,
        )
        self.registration.embargo.reload()
        assert_true(self.registration.embargo_end_date)
        assert_false(self.registration.pending_embargo)
        assert_equal(res.status_code, 302)

    # node_registration_embargo_disapprove tests
    def test_GET_from_unauthorized_user_returns_HTTPForbidden(self):
        unauthorized_user = AuthUserFactory()
        res = self.app.get(
            self.registration.web_url_for('node_registration_embargo_disapprove', token=fake.sentence()),
            auth=unauthorized_user.auth,
            expect_errors=True
        )
        assert_equal(res.status_code, 403)

    def test_GET_disapprove_registration_without_embargo_HTTPBad_Request(self):
        assert_false(self.registration.pending_embargo)
        res = self.app.get(
            self.registration.web_url_for('node_registration_embargo_disapprove', token=fake.sentence()),
            auth=self.user.auth,
            expect_errors=True
        )
        assert_equal(res.status_code, 400)

    def test_GET_disapprove_with_invalid_token_returns_HTTPBad_Request(self):
        self.registration.embargo_registration(
            self.user,
            datetime.datetime.utcnow() + datetime.timedelta(days=10)
        )
        self.registration.save()
        assert_true(self.registration.pending_embargo)

        res = self.app.get(
            self.registration.web_url_for('node_registration_embargo_disapprove', token=fake.sentence()),
            auth=self.user.auth,
            expect_errors=True
        )
        self.registration.embargo.reload()
        assert_true(self.registration.pending_embargo)
        assert_equal(res.status_code, 400)

    def test_GET_disapprove_with_wrong_admins_token_returns_HTTPBad_Request(self):
        admin2 = UserFactory()
        self.registration.contributors.append(admin2)
        self.registration.add_permission(admin2, 'admin', save=True)
        self.registration.embargo_registration(
            self.user,
            datetime.datetime.utcnow() + datetime.timedelta(days=10)
        )
        self.registration.save()
        assert_true(self.registration.pending_embargo)

        wrong_disapproval_token = self.registration.embargo.approval_state[admin2._id]['disapproval_token']
        res = self.app.get(
            self.registration.web_url_for('node_registration_embargo_disapprove', token=wrong_disapproval_token),
            auth=self.user.auth,
            expect_errors=True
        )
        assert_true(self.registration.pending_embargo)
        assert_equal(res.status_code, 400)

    def test_GET_disapprove_with_valid_token_returns_redirect_to_parent(self):
        project = ProjectFactory(creator=self.user)
        registration = RegistrationFactory(project=project)
        registration.embargo_registration(
            self.user,
            datetime.datetime.utcnow() + datetime.timedelta(days=10)
        )
        registration.save()
        assert_true(registration.pending_embargo)

        disapproval_token = registration.embargo.approval_state[self.user._id]['disapproval_token']
        res = self.app.get(
            registration.web_url_for('node_registration_embargo_disapprove', token=disapproval_token),
            auth=self.user.auth,
        )
        registration.embargo.reload()
        assert_equal(registration.embargo.state, Embargo.CANCELLED)
        assert_false(registration.embargo_end_date)
        assert_false(registration.pending_embargo)
        assert_equal(res.status_code, 302)
        assert_true(project._id in res.location)

    def test_GET_disapprove_for_existing_registration_with_valid_token_returns_redirect_to_registration(self):
        self.registration.embargo_registration(
            self.user,
            datetime.datetime.utcnow() + datetime.timedelta(days=10),
            for_existing_registration=True
        )
        self.registration.save()
        assert_true(self.registration.pending_embargo)

        disapproval_token = self.registration.embargo.approval_state[self.user._id]['disapproval_token']
        res = self.app.get(
            self.registration.web_url_for('node_registration_embargo_disapprove', token=disapproval_token),
            auth=self.user.auth,
        )
        self.registration.embargo.reload()
        assert_equal(self.registration.embargo.state, Embargo.CANCELLED)
        assert_false(self.registration.embargo_end_date)
        assert_false(self.registration.pending_embargo)
        assert_equal(res.status_code, 302)
        assert_true(self.registration._id in res.location)


class RegistrationEmbargoViewsTestCase(OsfTestCase):
    def setUp(self):
        super(RegistrationEmbargoViewsTestCase, self).setUp()
        ensure_schemas()
        self.user = AuthUserFactory()
        self.project = ProjectFactory(creator=self.user)
        self.registration = RegistrationFactory(project=self.project, creator=self.user)

        current_month = datetime.datetime.now().strftime("%B")
        current_year = datetime.datetime.now().strftime("%Y")

        self.valid_make_public_payload = json.dumps({
            u'embargoEndDate': u'Fri, 01, {month} {year} 00:00:00 GMT'.format(
                month=current_month,
                year=current_year
            ),
            u'registrationChoice': 'immediate',
            u'summary': unicode(fake.sentence())
        })
        valid_date = datetime.datetime.now() + datetime.timedelta(days=180)
        self.valid_embargo_payload = json.dumps({
            u'embargoEndDate': unicode(valid_date.strftime('%a, %d, %B %Y %H:%M:%S')) + u' GMT',
            u'registrationChoice': 'embargo',
            u'summary': unicode(fake.sentence())
        })
        self.invalid_embargo_date_payload = json.dumps({
            u'embargoEndDate': u"Thu, 01 {month} {year} 05:00:00 GMT".format(
                month=current_month,
                year=str(int(current_year)-1)
            ),
            u'registrationChoice': 'embargo',
            u'summary': unicode(fake.sentence())
        })

    @mock.patch('framework.tasks.handlers.enqueue_task')
    def test_POST_register_make_public_immediately_creates_public_registration(self, mock_enqueue):
        res = self.app.post(
            self.project.api_url_for('node_register_template_page_post', template=u'Open-Ended_Registration'),
            self.valid_make_public_payload,
            content_type='application/json',
            auth=self.user.auth
        )
        assert_equal(res.status_code, 201)

        registration = Node.find().sort('-registered_date')[0]

        assert_true(registration.is_registration)
        assert_true(registration.is_public)

    @mock.patch('framework.tasks.handlers.enqueue_task')
    def test_POST_register_make_public_immediately_makes_children_public(self, mock_enqueue):
        component = NodeFactory(
            creator=self.user,
            parent=self.project,
            title='Component'
        )
        subproject = ProjectFactory(
            creator=self.user,
            parent=self.project,
            title='Subproject'
        )
        subproject_component = NodeFactory(
            creator=self.user,
            parent=subproject,
            title='Subcomponent'
        )

        res = self.app.post(
            self.project.api_url_for('node_register_template_page_post', template=u'Open-Ended_Registration'),
            self.valid_make_public_payload,
            content_type='application/json',
            auth=self.user.auth
        )
        self.project.reload()
        # Last node directly registered from self.project
        registration = Node.load(self.project.node__registrations[-1])
        assert_true(registration.is_public)
        for node in registration.get_descendants_recursive():
            assert_true(node.is_registration)
            assert_true(node.is_public)

    @mock.patch('framework.tasks.handlers.enqueue_task')
    def test_POST_register_embargo_is_not_public(self, mock_enqueue):
        res = self.app.post(
            self.project.api_url_for('node_register_template_page_post', template=u'Open-Ended_Registration'),
            self.valid_embargo_payload,
            content_type='application/json',
            auth=self.user.auth
        )

        assert_equal(res.status_code, 201)

        registration = Node.find().sort('-registered_date')[0]

        assert_true(registration.is_registration)
        assert_false(registration.is_public)
        assert_true(registration.pending_registration)
        assert_is_not_none(registration.embargo)

    @mock.patch('framework.tasks.handlers.enqueue_task')
    def test_POST_invalid_embargo_end_date_returns_HTTPBad_Request(self, mock_enqueue):
        res = self.app.post(
            self.project.api_url_for('node_register_template_page_post', template=u'Open-Ended_Registration'),
            self.invalid_embargo_date_payload,
            content_type='application/json',
            auth=self.user.auth,
            expect_errors=True
        )

        assert_equal(res.status_code, 400)

    @mock.patch('framework.tasks.handlers.enqueue_task')
    def test_valid_POST_embargo_adds_to_parent_projects_log(self, mock_enquque):
        initial_project_logs = len(self.project.logs)
        res = self.app.post(
            self.project.api_url_for('node_register_template_page_post', template=u'Open-Ended_Registration'),
            self.valid_embargo_payload,
            content_type='application/json',
            auth=self.user.auth
        )
        self.project.reload()
        # Logs: Created, registered, embargo initiated
        assert_equal(len(self.project.logs), initial_project_logs + 1)
