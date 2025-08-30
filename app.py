import sqlite3
import datetime
import calendar
import csv
from io import StringIO, BytesIO
from flask import Flask, render_template, request, jsonify, g, send_file
from waitress import serve
import pytz
import click
from flask.cli import with_appcontext

app = Flask(__name__)
DATABASE = 'attendance.db'

# --- Timezone Helper ---
def get_ist_today():
    ist = pytz.timezone('Asia/Kolkata')
    return datetime.datetime.now(ist).date()

# --- Database Functions ---
def get_db():
    db = getattr(g, '_database', None)
    if db is None:
        db = g._database = sqlite3.connect(DATABASE)
        db.row_factory = sqlite3.Row
    return db

@app.teardown_appcontext
def close_connection(exception):
    db = getattr(g, '_database', None)
    if db is not None:
        db.close()

def init_db():
    db = get_db()
    with open('schema.sql', 'r') as f:
        db.executescript(f.read())
    db.commit()
    print("Database initialized.")

@app.cli.command('initdb')
@with_appcontext
def initdb_command():
    """Initialize the database."""
    init_db()

# --- Auth & Signup ---
@app.route('/api/signup', methods=['POST'])
def signup():
    data = request.json
    username = data.get('username')
    password = data.get('password')
    name = data.get('name')
    role = data.get('role')
    class_name = data.get('class_name')

    if not (username and password and name and role and class_name):
        return jsonify({'success': False, 'message': 'All fields required.'}), 400
    if role not in ['teacher', 'student']:
        return jsonify({'success': False, 'message': 'Invalid role.'}), 400

    db = get_db()
    try:
        # For teacher, create class if not exists
        if role == 'teacher':
            db.execute('INSERT OR IGNORE INTO classes (name) VALUES (?)', (class_name,))
            db.commit()
            class_id = db.execute('SELECT id FROM classes WHERE name = ?', (class_name,)).fetchone()['id']
            db.execute('INSERT INTO users (username, password, name, role, status, class_id) VALUES (?, ?, ?, ?, "pending", ?)',
                       (username, password, name, role, class_id))
            db.commit()
        elif role == 'student':
            class_row = db.execute('SELECT id FROM classes WHERE name = ?', (class_name,)).fetchone()
            if not class_row:
                return jsonify({'success': False, 'message': 'Class not found. Ask your teacher to signup first.'}), 400
            class_id = class_row['id']
            db.execute('INSERT INTO users (username, password, name, role, status, class_id) VALUES (?, ?, ?, ?, "pending", ?)',
                       (username, password, name, role, class_id))
            db.commit()
            user_id = db.execute('SELECT id FROM users WHERE username = ?', (username,)).fetchone()['id']
            db.execute('INSERT INTO students (user_id, class_id) VALUES (?, ?)', (user_id, class_id))
            db.commit()
        return jsonify({'success': True, 'message': 'Signup submitted. Await approval by admin.'})
    except sqlite3.IntegrityError:
        return jsonify({'success': False, 'message': 'Username or class already exists.'}), 409

@app.route('/api/login', methods=['POST'])
def login():
    data = request.json
    username = data.get('username')
    password = data.get('password')
    db = get_db()
    user = db.execute('SELECT * FROM users WHERE username = ? AND password = ?', (username, password)).fetchone()
    if user:
        if user['status'] != 'approved':
            return jsonify({'success': False, 'message': 'Account pending admin approval.'}), 403
        return jsonify({'success': True, 'role': user['role'], 'name': user['name'], 'user_id': user['id'], 'class_id': user['class_id']})
    return jsonify({'success': False, 'message': 'Invalid credentials.'}), 401

# --- Admin Functions ---
@app.route('/api/admin/pending_users', methods=['GET'])
def get_pending_users():
    db = get_db()
    users = db.execute('SELECT * FROM users WHERE status = "pending"').fetchall()
    return jsonify({'pending_users': [dict(u) for u in users]})

@app.route('/api/admin/approve_user', methods=['POST'])
def approve_user():
    user_id = request.json.get('user_id')
    db = get_db()
    db.execute('UPDATE users SET status = "approved" WHERE id = ?', (user_id,))
    db.commit()
    return jsonify({'success': True, 'message': 'User approved.'})

# --- Teacher Functions ---
@app.route('/api/teacher/students', methods=['GET'])
def get_teacher_students():
    teacher_id = request.args.get('teacher_id')
    db = get_db()
    class_row = db.execute('SELECT id FROM classes WHERE teacher_id = ?', (teacher_id,)).fetchone()
    # If not set, fallback to teacher's assigned class
    if not class_row:
        user_row = db.execute('SELECT class_id FROM users WHERE id = ?', (teacher_id,)).fetchone()
        if not user_row or not user_row['class_id']:
            return jsonify({'students': []})
        class_id = user_row['class_id']
    else:
        class_id = class_row['id']
    students = db.execute('SELECT students.id, users.name FROM students JOIN users ON students.user_id = users.id WHERE students.class_id = ?', (class_id,))
    return jsonify({'students': [dict(s) for s in students]})

@app.route('/api/teacher/mark_attendance', methods=['POST'])
def mark_attendance():
    data = request.json
    student_id = data.get('student_id')
    class_id = data.get('class_id')
    status = data.get('status')
    remarks = data.get('remarks', '')
    today = get_ist_today().isoformat()
    db = get_db()
    existing = db.execute('SELECT id FROM attendance WHERE student_id = ? AND class_id = ? AND date = ?', (student_id, class_id, today)).fetchone()
    if existing:
        db.execute('UPDATE attendance SET status = ?, remarks = ? WHERE id = ?', (status, remarks, existing['id']))
    else:
        db.execute('INSERT INTO attendance (student_id, class_id, date, status, remarks) VALUES (?, ?, ?, ?, ?)', (student_id, class_id, today, status, remarks))
    db.commit()
    return jsonify({'success': True, 'message': 'Attendance marked.'})

# --- Monthly Report & Export ---
@app.route('/api/teacher/monthly_report', methods=['GET'])
def monthly_report():
    class_id = request.args.get('class_id')
    month_str = request.args.get('month')
    db = get_db()
    students = db.execute('SELECT students.id, users.name FROM students JOIN users ON students.user_id = users.id WHERE students.class_id = ?', (class_id,))
    student_list = [dict(s) for s in students]
    year, month = map(int, month_str.split('-'))
    num_days = calendar.monthrange(year, month)[1]
    days_in_month = [f"{month_str}-{day:02d}" for day in range(1, num_days + 1)]
    report = []
    for student in student_list:
        record = {'name': student['name'], 'present': 0.0, 'absent': 0}
        for day in days_in_month:
            r = db.execute('SELECT status FROM attendance WHERE student_id = ? AND class_id = ? AND date = ?', (student['id'], class_id, day)).fetchone()
            if r:
                status = r['status']
                if status == 'Full Day': record['present'] += 1.0
                elif status == 'Half Day': record['present'] += 0.5
                elif status == 'Absent': record['absent'] += 1
        report.append(record)
    return jsonify({'students': student_list, 'report': report, 'days_in_month': days_in_month})

@app.route('/api/teacher/export_report', methods=['GET'])
def export_report():
    class_id = request.args.get('class_id')
    month_str = request.args.get('month')
    db = get_db()
    students = db.execute('SELECT students.id, users.name FROM students JOIN users ON students.user_id = users.id WHERE students.class_id = ?', (class_id,))
    student_list = [dict(s) for s in students]
    year, month = map(int, month_str.split('-'))
    num_days = calendar.monthrange(year, month)[1]
    days_in_month = [f"{month_str}-{day:02d}" for day in range(1, num_days + 1)]
    output = StringIO()
    writer = csv.writer(output)
    header = ['Student Name'] + [d.split('-')[2] for d in days_in_month] + ['Present', 'Absent']
    writer.writerow(header)
    for student in student_list:
        row = [student['name']]
        present = 0.0
        absent = 0
        for day in days_in_month:
            r = db.execute('SELECT status FROM attendance WHERE student_id = ? AND class_id = ? AND date = ?', (student['id'], class_id, day)).fetchone()
            if r:
                status = r['status']
                if status == 'Full Day':
                    row.append('F')
                    present += 1.0
                elif status == 'Half Day':
                    row.append('H')
                    present += 0.5
                elif status == 'Absent':
                    row.append('A')
                    absent += 1
            else:
                row.append('')
        row += [present, absent]
        writer.writerow(row)
    output_bytes = BytesIO(output.getvalue().encode('utf-8'))
    output_bytes.seek(0)
    return send_file(output_bytes, mimetype='text/csv', as_attachment=True, download_name=f'report_{month_str}.csv')

# --- Student Functions ---
@app.route('/api/student/attendance', methods=['POST'])
def student_attendance():
    user_id = request.json.get('user_id')
    db = get_db()
    student = db.execute('SELECT id, class_id FROM students WHERE user_id = ?', (user_id,)).fetchone()
    if not student:
        return jsonify({'error': 'Student not found.'}), 404
    records = db.execute('SELECT date, status, remarks FROM attendance WHERE student_id = ? ORDER BY date DESC', (student['id'],)).fetchall()
    present = 0.0
    absent = 0
    total = 0
    for r in records:
        if r['status'] == 'Full Day':
            present += 1.0
            total += 1
        elif r['status'] == 'Half Day':
            present += 0.5
            total += 1
        elif r['status'] == 'Absent':
            absent += 1
            total += 1
    percentage = (present / total * 100) if total > 0 else 0
    return jsonify({'records': [dict(x) for x in records], 'present': present, 'absent': absent, 'percentage': round(percentage)})

@app.route("/")
def index():
    return render_template("index.html")

if __name__ == '__main__':
    print("Starting production server at http://0.0.0.0:5000")
    serve(app, host='0.0.0.0', port=5000)
