# ElastAlert - [Docs](http://elastalert.readthedocs.org).
## Cài đặt.

### Cài đặt Python.
 1. Cài Đặt Python 3.8 và pip:
```bash
sudo apt update
sudo apt install -y python3.8 python3.8-venv python3.8-dev
sudo apt install -y python3-pip
```
2. Kích hoạt môi trường ảo:
```bash
python3.8 -m venv myenv
source myenv/bin/activate
```
3. Cài Đặt pip và Các Phụ Thuộc
```bash 
pip install --upgrade pip
```

### Cài đặt ElastAlert.
1. Clone Repo ElastAlert từ GitHub
```bash
git clone https://github.com/elastic/elastalert.git
cd elastalert
```
2. Cài đặt các module
```bash 
python3.8 setup.py install
pip install "elasticsearch>=7.0.0"
```
## Configuration
Tạo một file config.yaml
```bash
touch config.yaml
```
Ví dụ về file config:
```bash
rules_folder: rules ## trỏ đến file rules hoặc thư mục rules

# Thời gian ElastAlert thực hiện query lại elastic
# Unit có thể sử dụng weeks -> seconds
run_every:
  seconds: 10


buffer_time:
  minutes: 15

es_host: localhost

es_port: 9200

#config ssl
use_ssl: false

# Verify TLS certificates
verify_certs: False

# Show TLS or certificate related warnings
#ssl_show_warn: True
es_username: elastic
es_password: elastic
writeback_index: elastalert_status
alert_time_limit:
  days: 2

```
### Các Loại `Type` thường dùng:

- Khi có một sự kiện X trong thời gian Y " (``frequency`` type)
```bash
name: "Alert Frequency"
type: frequency
index: logstash-*
num_events: 50
timeframe:
  hours: 4
filter:
- query:
    query_string:
      query: "status:500"
alert:
- "email"
email:
- "you@gmail.com"
```

- Khi có sự tăng giảm đột ngột của một event" (``spike`` type)
```bash
name: "alert spike"
type: spike
index: logstash-*
threshold:
  hours: 2
spike_type: increase
spike_threshold: 3
filter:
- query:
    query_string:
      query: "error_type:critical"
alert:
- "slack"
slack:
  webhook_url: "https://hooks.slack.com/services/..."
```
- Khi có ít hơn sự kiện X trong một khoảng thời gian " (``flatline`` type)
```bash
name: "Warning flatline"
type: flatline
index: logstash-*
threshold: 10
timeframe:
  hours: 1
filter:
- query:
    query_string:
      query: "event_type:login"
alert:
- "email"
email:
- "you@example.com"

```


Hiện tại ElastAlert hỗ trợ cảnh báo qua những channel:

- Email
- JIRA
- OpsGenie
- Commands
- HipChat
- MS Teams
- Slack
- Telegram
- GoogleChat
- AWS SNS
- VictorOps
- PagerDuty
- PagerTree
- Exotel
- Twilio
- Gitter
- Line Notify
- Zabbix


## Example rules

```bash
name: Example frequency rule

# (Required)
# Type of alert.
# the frequency rule type alerts when num_events events occur with timeframe time
type: any

# (Required)
# Index to search, wildcard supported
index: http.zengi_trans_notify.success.2-2024.08.09
use_strftime_index: true

# (Required, frequency specific)
# Alert when this many documents matching the query occur within a timeframe
num_events: 50

# (Required, frequency specific)
# num_events must occur within this amount of time to trigger an alert
timeframe:
  hours: 4

# (Required)
# A list of Elasticsearch filters used for find events
# These filters are joined with AND and nested in a filtered query
# For more info: http://www.elasticsearch.org/guide/en/elasticsearch/reference/current/query-dsl.html
filter:
- term:
    data.isSuccess: true

timestamp_field: "data.partnerTransNotifyReport.createdDate"


# (Required)
# The alert is use when a match is found
alert:
- "email"

# (required, email specific)
# a list of email addresses to send alerts to
email:
- "trungtrann3108@gmail.com"

alert_text_type: alert_text_only

alert_text: "
Partner Code : {0}\n
Success : {1}\n
Timestamp : {2}"
alert_text_args: ["data.partnerTransNotifyReport.partnerCode","data.isSuccess","data.partnerTransNotifyReport.createdDate"]

smtp_host: smtp.gmail.com
smtp_port: 587
smtp_auth_file: smtp.auth.yml

```

### Test rule 

```bash
elastalert-test-rule --config path/to/config.yaml path/to/rule_file.yaml
```

### Run ElastAlert
```bash
python3.8 -m elastalert.elastalert --verbose --rule path/to/rule_file.yaml
```

Lưu ý khi tạo rule phải mapping field có giá trị timestamp với timestamp_field ở ví dụ trên là `timestamp_field: "data.partnerTransNotifyReport.createdDate"`