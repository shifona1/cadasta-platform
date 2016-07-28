import time
from zipfile import ZipFile
from django import forms
from django.conf import settings
from django.contrib.postgres import forms as pg_forms
from django.contrib.gis import forms as gisforms
from core.util import slugify
from django.utils.translation import ugettext as _
from django.db import transaction
from django.forms.utils import ErrorDict

from leaflet.forms.widgets import LeafletWidget
from tutelary.models import Role, check_perms
from buckets.widgets import S3FileUploadWidget

from accounts.models import User
from questionnaires.models import Questionnaire
from .models import Organization, Project, OrganizationRole, ProjectRole
from .choices import ADMIN_CHOICES, ROLE_CHOICES
from .fields import ProjectRoleField, PublicPrivateField, ContactsField
from .download.xls import XLSExporter
from .download.resources import ResourceExporter
from .download.shape import ShapeExporter

FORM_CHOICES = ROLE_CHOICES + (('Pb', _('Public User')),)
QUESTIONNAIRE_TYPES = [
    'application/msexcel',
    'application/vnd.ms-excel',
    'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'
]


def create_update_or_delete_project_role(project, user, role):
    if role != 'Pb':
        ProjectRole.objects.update_or_create(
            user=user,
            project_id=project,
            defaults={'role': role})
    else:
        ProjectRole.objects.filter(user=user,
                                   project_id=project).delete()


class ContactsForm(forms.Form):
    name = forms.CharField()
    email = forms.EmailField(required=False)
    tel = forms.CharField(required=False)
    remove = forms.BooleanField(required=False, widget=forms.HiddenInput())

    def as_table(self):
        html = self._html_output(
            normal_row='<td>%(field)s%(help_text)s</td>',
            error_row=('<tr class="contacts-error {error_types}">'
                       '<td colspan="4">%s</td></tr>\n<tr>'),
            row_ender='</td>',
            help_text_html='<br /><span class="helptext">%s</span>',
            errors_on_separate_row=False)
        closeBtn = ('<td><a data-prefix="' + self.prefix + '" '
                    'class="close remove-contact" href="#">'
                    '<span aria-hidden="true">&times;</span></a></td>')
        html = ('' if self.errors else '<tr>') + html + closeBtn + '</tr>\n'

        error_types = ''
        if 'name' in self.errors:
            error_types += ' error-name'
        if 'email' in self.errors:
            error_types += ' error-email'
        if hasattr(self, 'contact_details_is_missing'):
            error_types += ' error-email error-phone'

        return html.format(error_types=error_types)

    def full_clean(self):
        if self.data.get(self.prefix + '-remove') != 'on':
            super().full_clean()
        else:
            self._errors = ErrorDict()
            self.cleaned_data = {'remove': True}

    def clean(self):
        cleaned_data = super().clean()
        error_msgs = []
        if 'name' in self.errors:
            error_msgs.append(_("Please provide a name."))
        if 'email' in self.errors:
            error_msgs.append(_("The provided email address is invalid."))
        if (
            not self.errors and
            not cleaned_data['email'] and
            not cleaned_data['tel']
        ):
            self.contact_details_is_missing = True
            error_msgs.append(_(
                "Please provide either an email address or a phone number."))
        if error_msgs:
            raise forms.ValidationError(" ".join(error_msgs))
        return cleaned_data

    def clean_string(self, value):
        if not value:
            return None
        return value

    def clean_email(self):
        return self.clean_string(self.cleaned_data['email'])

    def clean_tel(self):
        return self.clean_string(self.cleaned_data['tel'])


class OrganizationForm(forms.ModelForm):
    urls = pg_forms.SimpleArrayField(forms.URLField(), required=False)
    contacts = ContactsField(form=ContactsForm, required=False)
    access = PublicPrivateField()

    class Meta:
        model = Organization
        fields = ['name', 'description', 'urls', 'contacts', 'access']

    def __init__(self, *args, **kwargs):
        self.user = kwargs.pop('user', None)
        super(OrganizationForm, self).__init__(*args, **kwargs)

    def to_list(self, value):
        if value:
            return [value]
        else:
            return []

    def clean_urls(self):
        return self.to_list(self.data.get('urls'))

    def clean_name(self):
        is_create = not self.instance.id
        name = self.cleaned_data['name']
        invalid_names = settings.CADASTA_INVALID_ENTITY_NAMES
        if is_create and slugify(name, allow_unicode=True) in invalid_names:
            raise forms.ValidationError(
                _("Organization name cannot be “Add” or “New”."))
        return name

    def save(self, *args, **kwargs):
        instance = super(OrganizationForm, self).save(commit=False)
        is_create = not instance.id

        instance.save()

        if is_create:
            OrganizationRole.objects.create(
                organization=instance,
                user=self.user,
                admin=True
            )

        return instance


class AddOrganizationMemberForm(forms.Form):
    identifier = forms.CharField()

    def __init__(self, *args, **kwargs):
        self.organization = kwargs.pop('organization', None)
        self.instance = kwargs.pop('instance', None)
        super(AddOrganizationMemberForm, self).__init__(*args, **kwargs)

    def clean_identifier(self):
        identifier = self.data.get('identifier')
        try:
            self.user = User.objects.get_from_username_or_email(
                identifier=identifier)
        except (User.DoesNotExist, User.MultipleObjectsReturned) as e:
            raise forms.ValidationError(e)
        user_exists = OrganizationRole.objects.filter(
            user=self.user, organization=self.organization).exists()
        if user_exists:
            raise forms.ValidationError(
                _("User is already a member of the organization."),
                code='member_already')

    def save(self):
        if self.errors:
            raise ValueError(
                "The role could not be assigned because the data didn't "
                "validate."
            )

        self.instance = OrganizationRole.objects.create(
            organization=self.organization, user=self.user)
        return self.instance


class EditOrganizationMemberForm(forms.Form):
    org_role = forms.ChoiceField(choices=ADMIN_CHOICES)

    def __init__(self, data, organization, user, *args, **kwargs):
        super(EditOrganizationMemberForm, self).__init__(data, *args, **kwargs)
        self.data = data
        self.organization = organization
        self.user = user

        self.org_role_instance = OrganizationRole.objects.get(
            user=user,
            organization=self.organization)

        self.initial['org_role'] = 'A' if self.org_role_instance.admin else 'M'

        project_roles = ProjectRole.objects.filter(
            project__organization=organization).values('project__id', 'role')
        project_roles = {r['project__id']: r['role'] for r in project_roles}

        for p in self.organization.projects.values_list('id', 'name'):
            role = project_roles.get(p[0], 'Pb')

            self.fields[p[0]] = forms.ChoiceField(
                choices=FORM_CHOICES,
                label=p[1],
                required=False,
                initial=role
            )

    def save(self):
        self.org_role_instance.admin = self.data.get('org_role') == 'A'
        self.org_role_instance.save()

        for f in [field for field in self.fields if field != 'org_role']:
            role = self.data.get(f)
            create_update_or_delete_project_role(f, self.user, role)


class ProjectAddExtents(forms.ModelForm):
    extent = gisforms.PolygonField(widget=LeafletWidget(), required=False)

    class Meta:
        model = Project
        fields = ['extent']


class ProjectAddDetails(forms.Form):
    organization = forms.ChoiceField()
    name = forms.CharField(max_length=100)
    description = forms.CharField(required=False, widget=forms.Textarea)
    access = PublicPrivateField(initial='public')
    url = forms.URLField(required=False)
    questionaire = forms.CharField(
        required=False,
        widget=S3FileUploadWidget(upload_to='xls-forms',
                                  accepted_types=QUESTIONNAIRE_TYPES))
    contacts = ContactsField(form=ContactsForm, required=False)

    def check_admin(self, user):
        if not hasattr(self, 'su_role'):
            self.su_role = Role.objects.get(name='superuser')

        is_superuser = any([isinstance(pol, Role) and pol == self.su_role
                            for pol in user.assigned_policies()])
        return is_superuser

    def __init__(self, *args, **kwargs):
        self.user = kwargs.pop('user', None)
        super().__init__(*args, **kwargs)

        if self.check_admin(self.user):
            self.fields['organization'].choices = [
                (o.slug, o.name) for o in Organization.objects.order_by('name')
            ]
        else:
            qs = self.user.organizations.all()
            self.fields['organization'].choices = [
                (o.slug, o.name) for o in qs.order_by('name')
                if check_perms(self.user, ('project.create',), (o,))
            ]

    def clean_name(self):
        name = self.cleaned_data['name']
        invalid_names = settings.CADASTA_INVALID_ENTITY_NAMES
        if slugify(name, allow_unicode=True) in invalid_names:
            raise forms.ValidationError(
                _("Project name cannot be “Add” or “New”."))
        return name


class ProjectEditDetails(forms.ModelForm):
    urls = pg_forms.SimpleArrayField(forms.URLField(), required=False)
    questionnaire = forms.CharField(
        required=False,
        widget=S3FileUploadWidget(upload_to='xls-forms',
                                  accepted_types=QUESTIONNAIRE_TYPES))
    access = PublicPrivateField()
    contacts = ContactsField(form=ContactsForm, required=False)

    class Meta:
        model = Project
        fields = ['name', 'description', 'access', 'urls', 'questionnaire',
                  'contacts']

    def save(self, *args, **kwargs):
        new_form = self.data.get('questionnaire')
        current_form = self.initial.get('questionnaire')

        if new_form:
            if current_form != new_form:
                Questionnaire.objects.create_from_form(
                    xls_form=new_form,
                    project=self.instance
                )
        else:
            self.instance.current_questionnaire = ''

        return super().save(*args, **kwargs)


class PermissionsForm:
    def check_admin(self, user):
        if not hasattr(self, 'su_role'):
            self.su_role = Role.objects.get(name='superuser')

        if not hasattr(self, 'admins'):
            self.admins = [
                role.user for role in OrganizationRole.objects.filter(
                    organization=self.organization,
                    admin=True
                )
            ]

        is_superuser = any([isinstance(pol, Role) and pol == self.su_role
                            for pol in user.assigned_policies()])
        return is_superuser or user in self.admins

    def set_fields(self):
        for user in self.organization.users.all():
            if self.check_admin(user):
                role = 'A'
            else:
                if hasattr(self, 'project_roles'):
                    role = self.project_roles.get(user.id, 'Pb')
                else:
                    role = 'Pb'

            self.fields[user.username] = ProjectRoleField(
                choices=FORM_CHOICES,
                label=user.full_name,
                required=(role != 'A'),
                initial=role,
                user=user
            )


class ProjectAddPermissions(PermissionsForm, forms.Form):
    def __init__(self, organization, *args, **kwargs):
        super().__init__(*args, **kwargs)
        if organization is not None:
            self.organization = Organization.objects.get(slug=organization)
            self.set_fields()


class ProjectEditPermissions(PermissionsForm, forms.Form):
    def __init__(self, *args, **kwargs):
        self.project = kwargs.pop('instance')
        super().__init__(*args, **kwargs)
        self.organization = self.project.organization

        project_roles = ProjectRole.objects.filter(
            project=self.project).values('user__id', 'role')
        self.project_roles = {r['user__id']: r['role'] for r in project_roles}
        self.set_fields()

    def save(self):
        with transaction.atomic():
            for k, f in [
                (k, f) for k, f in self.fields.items() if f.initial != 'A'
            ]:
                role = self.data.get(k)
                create_update_or_delete_project_role(
                    self.project.id, f.user, role)
        return self.project


class DownloadForm(forms.Form):
    CHOICES = (('all', 'All data'), ('xls', 'XLS'), ('shp', 'SHP'),
               ('res', 'Resources'))
    type = forms.ChoiceField(choices=CHOICES, initial='xls')

    def __init__(self, project, user, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.project = project
        self.user = user

    def get_file(self):
        t = round(time.time() * 1000)

        file_name = '{}-{}-{}'.format(self.project.id, self.user.id, t)
        type = self.cleaned_data['type']

        if type == 'shp':
            e = ShapeExporter(self.project)
            path, mime = e.make_download(file_name + '-shp')
        elif type == 'xls':
            e = XLSExporter(self.project)
            path, mime = e.make_download(file_name + '-xls')
        elif type == 'res':
            e = ResourceExporter(self.project)
            path, mime = e.make_download(file_name + '-res')
        elif type == 'all':
            res_exporter = ResourceExporter(self.project)
            xls_exporter = XLSExporter(self.project)
            shp_exporter = ShapeExporter(self.project)
            path, mime = res_exporter.make_download(file_name + '-res')
            data_path, _ = xls_exporter.make_download(file_name + '-xls')
            shp_path, _ = shp_exporter.make_download(file_name + '-shp')

            with ZipFile(path, 'a') as myzip:
                myzip.write(data_path, arcname='data.xlsx')
                myzip.write(shp_path, arcname='data-shp.zip')
                myzip.close()

        return path, mime
