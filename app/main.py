import datetime
from flask import jsonify, json, Response, request, Flask, render_template, session, abort, redirect
from flask_cors import CORS, cross_origin
import pymysql
import os
import pathlib
import requests
from google.oauth2 import id_token, service_account
from google_auth_oauthlib.flow import Flow
from pip._vendor import cachecontrol
import google.auth.transport.requests
import time, json
from pprint import pprint
from Google import Create_Service, convert_to_RFC_datetime
from datetime import datetime
import pickle
import smtplib, email
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.mime.base import MIMEBase
from email import encoders
from google.cloud import storage
import subprocess
###########################################
# REDIS DEPLOYMENT CONFIGURATION
# redis_host = os.environ.get('REDISHOST', 'localhost')
# redis_port = int(os.environ.get('REDISPORT', 6379))
# redis_client = redis.Redis(host=redis_host, port=redis_port)
# get_feedback_cache_key = 'feedback_data'
# get_user_cache_key = 'user_medical_history'
###########################################

###########################################
# Calendar API
###########################################
CLIENT_SECRET_FILE = os.path.join(pathlib.Path(__file__).parent, "client_secret_calendar.json")
API_NAME = 'calendar'
API_VERSION = 'v3'
SCOPES = ['https://www.googleapis.com/auth/calendar']
service = Create_Service(CLIENT_SECRET_FILE, API_NAME, API_VERSION, SCOPES)
calendar_id = 'eshaflynn@gmail.com'
###########################################

###########################################
# Local DB Connection
###########################################
db_user = os.environ.get('CLOUD_SQL_USERNAME')
db_password = os.environ.get('CLOUD_SQL_PASSWORD')
db_name = os.environ.get('CLOUD_SQL_DATABASE_NAME')
db_connection_name = os.environ.get('CLOUD_SQL_CONNECTION_NAME')

if os.environ.get('GAE_ENV') != 'standard':
    db_user = 'root'
    db_password = '1234'
    db_name = 'medicine'
    db_connection_name = ''
###########################################

###########################################
# Local DB Connection
###########################################
CLOUD_STORAGE_BUCKET = 'medicine-expiry-reminder'
DOWNLOAD_DST = 'QrCodeUploadFolder'
###########################################

###########################################
# Google Oauth2
###########################################
app = Flask(__name__)
app.secret_key = "medicineexpiryreminder.com"
CORS(app)

os.environ["OAUTHLIB_INSECURE_TRANSPORT"] = "1"

GOOGLE_CLIENT_ID = ""
client_secrets_file = os.path.join(pathlib.Path(__file__).parent, "client_secret.json")
###########################################


###########################################
# Google Application Credential
###########################################
gcp_credential = os.path.join(pathlib.Path(__file__).parent, "gcp-project.json")
os.environ["GOOGLE_APPLICATION_CREDENTIALS"] = gcp_credential
###########################################
# Google EMail API
###########################################
ADMIN_MAIL_ID = ""
ADMIN_MAIL_PASSWORD = ""
##########################################
flow = Flow.from_client_secrets_file(
    client_secrets_file=client_secrets_file,
    scopes=["https://www.googleapis.com/auth/userinfo.profile", "https://www.googleapis.com/auth/userinfo.email",
            "openid"],
    redirect_uri=""
)


def custom_response(res, status_code):
    """
    Custom Response Function
    """
    return Response(
        mimetype="application/json",
        response=json.dumps(res),
        status=status_code
    )


def login_is_required(function):
    def wrapper(*args, **kwargs):
        if "google_id" not in session:
            return "You need to login first"
        else:
            return function()

    wrapper.__name__ = function.__name__
    return wrapper


@app.route("/callback")
def callback():
    flow.fetch_token(authorization_response=request.url)

    if not session["state"] == request.args["state"]:
        abort(500)  # State does not match!

    credentials = flow.credentials
    request_session = requests.session()
    cached_session = cachecontrol.CacheControl(request_session)
    token_request = google.auth.transport.requests.Request(session=cached_session)

    id_info = id_token.verify_oauth2_token(
        id_token=credentials._id_token,
        request=token_request,
        audience=GOOGLE_CLIENT_ID
    )

    session["google_id"] = id_info.get("sub")
    session["name"] = id_info.get("name")
    session["email"] = id_info.get("email")
    return redirect("/qrcodescanner")


@app.route('/signin', methods=['GET'])
def signin():
    authorization_url, state = flow.authorization_url()
    session["state"] = state
    return redirect(authorization_url)


@app.route('/')
def main():
    if "google_id" in session:
        return redirect("/qrcodescanner")
    else:
        return render_template('index.html')

@app.route('/qrcodescanner')
@login_is_required
def qrcodescanner():
    return render_template('qrcodescanner.html')


@app.route("/signout")
def logout():
    session.clear()
    return redirect("/")


@app.route("/medicinefeedback")
@login_is_required
def medicinefeedback():
    return render_template('medicinefeedback.html')

@app.route("/deletereminder")
@login_is_required
def deletereminder():
    return render_template('deletereminder.html')


@app.route('/extractqrcodedata', methods=['POST'])
@login_is_required
def extractqrcodedata():
    data = request.get_json()
    qr_code_data = json.loads(data['qrcodedata'])

    billId = qr_code_data['id']
    res = checkQrCodeExistinDB(billId)
    if not res:
        qr_data = qr_code_data['medicine_info']
        for i in qr_data:
            response = addReminderInCalendar(i['name'], i['expiry'])
            insert_qr_code_data(qr_code_data, response, i['name'], i['expiry'], i['company_name'])
        return custom_response("Success", 200)

    return custom_response("QrCode is already processed", 201)

def insert_qr_code_data(qr_data, apiResponse, medicineName, medicineExpiry, companyName):
    billId = qr_data['id']
    userEmailId = session["email"]
    doctorName = qr_data['doctor_name']
    doctorEmailId = qr_data['doctor_email']
    eventId = apiResponse['id']
    if os.environ.get('GAE_ENV') == 'standard':
        unix_socket = '/cloudsql/{}'.format(db_connection_name)
        cnx = pymysql.connect(user=db_user, password=db_password, unix_socket=unix_socket, db=db_name)
    else:
        host = '127.0.0.1'
        cnx = pymysql.connect(user=db_user, password=db_password, host=host, db=db_name)
    with cnx.cursor() as cursor:
        expiry_date = datetime.strptime(medicineExpiry, '%m/%d/%Y')
        cursor.execute(
            "Insert into medicine_bill(billId, userEmailId, doctorName, doctorEmailId, medicineName, medicineExpiry, companyName, calendarEventId) Values (%s,%s,%s,%s,%s,%s,%s,%s)",
            (billId, userEmailId, doctorName, doctorEmailId, medicineName, expiry_date, companyName, eventId))
        cnx.commit()
        cnx.close()

@app.route('/autoscaling', methods=['POST'])
def autoscaling():
    data = request.get_json()
    qr_code_data = json.loads(data['qrcodedata'])
    billId = qr_code_data['id']
    if os.environ.get('GAE_ENV') == 'standard':
        unix_socket = '/cloudsql/{}'.format(db_connection_name)
        cnx = pymysql.connect(user=db_user, password=db_password, unix_socket=unix_socket, db=db_name)
    else:
        host = '127.0.0.1'
        cnx = pymysql.connect(user=db_user, password=db_password, host=host, db=db_name)
    with cnx.cursor() as cursor:
        cursor.execute(
            "Insert into autoscaling(billId) Values (%s)", (billId))
        cnx.commit()
        cnx.close()

@app.route('/medicinereviews')
@login_is_required
def medicinereviews():
    return render_template('medicinereviews.html')


@app.route('/getmedicinefeedback', methods=['GET'])
def getmedicinefeedback():
    # cacheData = redis_client.lrange(get_feedback_cache_key, 0, 0)
    record = {}
    # if len(cacheData) != 0:
    #     print('CacheDate: ' + str(cacheData))
    #     result = pickle.loads(cacheData[0])
    if os.environ.get('GAE_ENV') == 'standard':
        unix_socket = '/cloudsql/{}'.format(db_connection_name)
        cnx = pymysql.connect(user=db_user, password=db_password, unix_socket=unix_socket, db=db_name)
    else:
        host = '127.0.0.1'
        cnx = pymysql.connect(user=db_user, password=db_password, host=host, db=db_name)
    with cnx.cursor() as cursor:
        cursor.execute('Select * from medicine_feedback where postfeedback = \'Yes\' Order by feedbackdate')
        result = cursor.fetchall()
        # redis_client.lpush(get_feedback_cache_key, pickle.dumps(result))
        # redis_client.expire(get_feedback_cache_key, 10)
        cnx.commit()
    count = 1
    for res in result:
        temp = {
            'fullname': res[0],
            'medicineName': res[1],
            'pharmaceuticalCompany': res[2],
            'feedback': res[4],
            'date': str(res[6])
        }

        record[count] = temp
        count += 1

    return custom_response(record, 200)


@app.route('/getUserMedicineHistory', methods=['GET'])
def getUserMedicineHistory():
    # cacheData = redis_client.lrange(get_user_cache_key, 0, 0)
    record = {}
    # if len(cacheData) != 0:
    #     print('CacheDate: ' + str(cacheData))
    #     result = pickle.loads(cacheData[0])
    # else:
    # print('DB Hit')
    if os.environ.get('GAE_ENV') == 'standard':
        unix_socket = '/cloudsql/{}'.format(db_connection_name)
        cnx = pymysql.connect(user=db_user, password=db_password, unix_socket=unix_socket, db=db_name)
    else:
        host = '127.0.0.1'
        cnx = pymysql.connect(user=db_user, password=db_password, host=host, db=db_name)
    with cnx.cursor() as cursor:
        cursor.execute('Select * from medicine_bill where userEmailId = %s', (str(session["email"])), )
        result = cursor.fetchall()
        # redis_client.lpush(get_user_cache_key, pickle.dumps(result))
        # redis_client.expire(get_user_cache_key, 10)
        cnx.commit()
    count = 1
    for res in result:
        temp = {
            'medicineName': res[4],
            'pharmaceuticalCompany': res[6],
            'doctorName': res[2],
            'doctorMailId': res[3],
            'expiryDate': str(res[5])
        }

        record[count] = temp
        count += 1

    return custom_response(record, 200)


@app.route('/feedback', methods=['POST'])
def feedback():
    recs = request.get_json()
    fullname = session["name"]
    medicineName = recs['MedicineName']
    pharmaceuticalCompany = recs['PharmaceuticalCompany']
    emailId = recs['EmailId']
    feedback = recs['Feedback']
    postfeedback = recs['postfeedback']

    sendEmail(feedback, emailId)
    if os.environ.get('GAE_ENV') == 'standard':
        unix_socket = '/cloudsql/{}'.format(db_connection_name)
        cnx = pymysql.connect(user=db_user, password=db_password, unix_socket=unix_socket, db=db_name)
    else:
        host = '127.0.0.1'
        cnx = pymysql.connect(user=db_user, password=db_password, host=host, db=db_name)
    with cnx.cursor() as cursor:
        cursor.execute(
            "Insert into medicine_feedback (fullname,medicineName,pharmaceuticalCompany,emailId,feedback,postfeedback) Values (%s,%s,%s,%s,%s,%s)",
            (fullname, medicineName, pharmaceuticalCompany, emailId, feedback, postfeedback))
        cnx.commit()
        cnx.close()
        return custom_response("Success", 200)



@app.route('/dialogflow', methods = ['GET', 'POST'])
def qrcodescanner1():
    print(request.json['queryResult'])
    intent_name=request.json['queryResult']['intent']['displayName']
    print("Intent Name::")
    print(intent_name)
    data = {
            "fulfillmentText":"This is a text response"
            }
    return custom_response(data,200)



def addReminderInCalendar(medicineName, expiryDate):
    email = session["email"]
    date = expiryDate.split('/')
    event_request_body = {
        'start': {
            'dateTime': convert_to_RFC_datetime(int(date[2]), int(date[0]), int(date[1]), 12 + 1, 30),
            'timeZone': 'America/Los_Angeles'
        },
        'end': {
            'dateTime': convert_to_RFC_datetime(int(date[2]), int(date[0]), int(date[1]), 12 + 8, 30),
            'timeZone': 'America/Los_Angeles'
        },
        'summary': medicineName + ' expiring today',
        'description': 'Some items are expiring today',
        'colorId': 5,
        'status': 'confirmed',
        'transparency': 'opaque',
        'visibility': 'private',
        'location': 'Tempe, AZ',
        'attendees': [
            {
                'displayName': 'JJ',
                'comment': 'This is reminder from medicine expiry reminder system.',
                'email': email,
                'optional': False,
                'organiser': True,
                'responseStatus': 'accepted'
            }
        ],
    }

    maxAttendees = 5
    sendNotification = True
    sendUpdate = 'none'
    supportsAttachments = True

    response = service.events().insert(
        calendarId=calendar_id,
        maxAttendees=maxAttendees,
        sendNotifications=sendNotification,
        sendUpdates=sendUpdate,
        supportsAttachments=supportsAttachments,
        body=event_request_body
    ).execute()

    return response


@app.route('/uploadBills', methods=['POST'])
def upload():
    already_processed_qr_code = []
    if not os.path.exists(DOWNLOAD_DST):
        os.makedirs(DOWNLOAD_DST)
    files = request.files.getlist('files[]')
    if len(files) == 0:
        return custom_response("Failure", 500)
    gcs = storage.Client()
    bucket = gcs.get_bucket(CLOUD_STORAGE_BUCKET)
    for file in files:
        blob = bucket.blob(file.filename)
        blob.upload_from_string(
            file.read(),
            content_type=file.content_type
        )
        with open(file.filename, 'wb') as f:
            blob.download_to_file(f)

        proc = subprocess.Popen(['java', '-jar', 'qrcodedecoder.jar', file.filename], stdout=subprocess.PIPE, shell=True)
        (out, err) = proc.communicate()
        qr_data = str(str(out.strip())[2:-1])
        qr_code_data = json.loads(qr_data)
        billId = qr_code_data['id']

        res = checkQrCodeExistinDB(billId)

        if not res:
            qr_data = qr_code_data['medicine_info']
            for i in qr_data:
                response = addReminderInCalendar(i['name'], i['expiry'])
                insert_qr_code_data(qr_code_data, response, i['name'], i['expiry'], i['company_name'])
        else:
            already_processed_qr_code.append(file.filename)

        os.remove(file.filename)

    if len(already_processed_qr_code) == 0:
        return custom_response("Success", 200)

    return custom_response(already_processed_qr_code, 201)

@app.route('/deletereminderfromcalendar', methods=['POST'])
def delete_reminder_from_calendar():
    recs = request.get_json()
    medicineName = recs['MedicineName']
    pharmaceuticalCompany = recs['PharmaceuticalCompany']
    doctorEmailId = recs['EmailId']
    doctorName = recs['DoctorName']
    expiryDate = recs['ExpiryDate']
    if os.environ.get('GAE_ENV') == 'standard':
        unix_socket = '/cloudsql/{}'.format(db_connection_name)
        cnx = pymysql.connect(user=db_user, password=db_password, unix_socket=unix_socket, db=db_name)
    else:
        host = '127.0.0.1'
        cnx = pymysql.connect(user=db_user, password=db_password, host=host, db=db_name)
    with cnx.cursor(pymysql.cursors.DictCursor) as cursor:
        cursor.execute("Select * from medicine_bill where userEmailId = %s and doctorName = %s and doctorEmailId = %s and medicineName = %s and companyName = %s and medicineExpiry = %s",
                       (session["email"], doctorName, doctorEmailId, medicineName, pharmaceuticalCompany, expiryDate))
        result = cursor.fetchall()
        for record in result:
            deleteReminderFromCalendar(record['calendarEventId'])
            cursor.execute("Delete from medicine_bill where calendarEventId = %s", (record['calendarEventId'],))
        cnx.commit()
    return custom_response('Success', 200)

@app.route('/deleteallreminderfromcalendar', methods=['POST'])
def deleteallreminderfromcalendar():
    if os.environ.get('GAE_ENV') == 'standard':
        unix_socket = '/cloudsql/{}'.format(db_connection_name)
        cnx = pymysql.connect(user=db_user, password=db_password, unix_socket=unix_socket, db=db_name)
    else:
        host = '127.0.0.1'
        cnx = pymysql.connect(user=db_user, password=db_password, host=host, db=db_name)
    with cnx.cursor(pymysql.cursors.DictCursor) as cursor:
        cursor.execute("Select calendarEventId from medicine_bill where userEmailId = %s", (session["email"],))
        result = cursor.fetchall()
        for record in result:
            deleteReminderFromCalendar(record['calendarEventId'])

        cursor.execute("Delete from medicine_bill where userEmailId = %s", (session["email"],))
        cnx.commit()
    return custom_response('Success', 200)

def deleteReminderFromCalendar(calendarEventId):
    service.events().delete(calendarId=calendar_id, eventId=calendarEventId).execute()

def checkQrCodeExistinDB(billId):
    if os.environ.get('GAE_ENV') == 'standard':
        unix_socket = '/cloudsql/{}'.format(db_connection_name)
        cnx = pymysql.connect(user=db_user, password=db_password, unix_socket=unix_socket, db=db_name)
    else:
        host = '127.0.0.1'
        cnx = pymysql.connect(user=db_user, password=db_password, host=host, db=db_name)
    with cnx.cursor() as cursor:
        cursor.execute("Select * from medicine_bill where billId = %s and userEmailId = %s",
                       (str(billId), str(session["email"])), )
        result = cursor.fetchall()
        cnx.commit()
        if not result:
            return False
        else:
            return True

def sendEmail(msg_body, doctor_mail_id):
    fromaddr = ADMIN_MAIL_ID
    toaddr = doctor_mail_id
    msg = MIMEMultipart()
    msg['From'] = fromaddr
    msg['To'] = toaddr
    msg['Subject'] = "Feedback of the medicine from your patient"
    body = msg_body
    msg.attach(MIMEText(body, 'plain'))
    s = smtplib.SMTP('smtp.gmail.com', 587)
    s.starttls()
    s.login(fromaddr, ADMIN_MAIL_PASSWORD)
    text = msg.as_string()
    s.sendmail(fromaddr, toaddr, text)
    s.quit()

if __name__ == '__main__':
    app.run(host='127.0.0.1', port=8085, debug=True)
