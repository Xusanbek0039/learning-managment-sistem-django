"""Guruh davomati Excel eksporti: fayl to'liq yaratiladi, huquqsiz foydalanuvçi ololmaydi."""
from io import BytesIO
from unittest import mock

import openpyxl
from django.test import TestCase
from django.urls import reverse

from accounts.models import Profession, StudentAttendance
from accounts.tests_study_groups import TODAY, mk_group, mk_user
from accounts.tests_weather_effect import login


@mock.patch('telegram_bot.utils.notify_user', lambda *a, **k: None)
class AttendanceExcelExportTests(TestCase):
    def setUp(self):
        self.course = Profession.objects.create(name='Python', photo='p.jpg', description='-')
        self.teacher = mk_user('tch', 'teacher', 1)
        self.admin = mk_user('adm', 'admin', 2)
        self.student = mk_user('s1', 'student', 3)
        self.group = mk_group(self.course, self.teacher, start=-40)
        self.group.students.add(self.student)
        StudentAttendance.objects.create(group=self.group, student=self.student, date=TODAY,
                                         status='present', marked_by=self.teacher)
        self.url = reverse('attendance_report_excel', args=[self.group.pk])
        self.query = {'year': TODAY.year, 'month': TODAY.month}

    def test_admin_downloads_complete_workbook(self):
        login(self.client, self.admin)
        response = self.client.get(self.url, self.query)
        self.assertEqual(response.status_code, 200)
        self.assertIn('.xlsx', response['Content-Disposition'])
        ws = openpyxl.load_workbook(BytesIO(response.content)).active
        self.assertEqual(ws.cell(row=5, column=2).value, self.student.get_full_name())

    def test_teacher_is_redirected(self):
        login(self.client, self.teacher)
        response = self.client.get(self.url, self.query)
        self.assertEqual(response.status_code, 302)
