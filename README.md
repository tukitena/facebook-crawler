# Facebook Crawler

**Repository:** [tukitena/facebook-crawler](https://github.com/tukitena/facebook-crawler)

Đây là bài tập của lớp **CS232.R11**, Trường Đại học Công nghệ Thông tin, Đại học Quốc gia Thành phố Hồ Chí Minh (**UIT – VNUHCM**). Yêu cầu của bài tập là xây dựng công cụ thu thập bài viết từ trang [UIT Confessions](https://www.facebook.com/UITconfess), tách từng confession và gán nhãn sentiment **Positive**, **Negative**, **Neutral** theo nội dung đánh giá về UIT, từ đó tạo bộ dữ liệu có nhãn phục vụ học tập và nghiên cứu.

Một bài viết Facebook có thể chứa nhiều confession. Crawler lưu bài viết thô trước; bước tiền xử lý tách và loại trùng để mỗi confession trở thành một mẫu dữ liệu riêng.

> **Trạng thái hiện tại:** Đã có các lệnh `crawl`, `preprocess`, `classify` và `reset`. Bước gán nhãn sử dụng **Qwen chạy cục bộ qua Ollama**, hỗ trợ chạy tiếp, thử lại khi lỗi và đánh dấu mẫu cần kiểm tra thủ công.

## Mục lục

- [Dependencies](#dependencies)
- [Hướng dẫn cài đặt](#huong-dan-cai-dat)
- [Hướng dẫn sử dụng](#huong-dan-su-dung)
  - [Cấu hình](#cau-hinh)
  - [Thu thập dữ liệu — crawl](#crawl)
  - [Tiếp tục khi tốc độ crawl giảm](#tiep-tuc-crawl)
  - [Tiền xử lý — preprocess](#preprocess)
  - [Gán nhãn sentiment — classify](#classify)
  - [Dữ liệu đầu ra](#du-lieu)
  - [Xóa dataset để thu thập lại — reset](#reset)
- [Câu hỏi thường gặp](#cau-hoi-thuong-gap)
- [Cam kết và miễn trừ](#cam-ket-va-mien-tru)

<a id="dependencies"></a>

## Dependencies

### Môi trường

- **Python:** Mục tiêu thiết kế là Python 3.12 trở lên; môi trường đã kiểm thử của dự án là **Python 3.14.2 trên Windows**. Python 3.12 chưa được kiểm thử trực tiếp.
- **Google Chrome:** Cần cài trên máy để chạy crawler. Chương trình dùng Chrome với profile riêng trong `runtime/chrome-profile/`.
- **Git:** Dùng để clone repository; có thể tải mã nguồn dạng ZIP nếu không dùng Git.
- **Internet và tài khoản Facebook** có thể truy cập trang cần thu thập. Người dùng tự đăng nhập và xác minh tài khoản trong Chrome.
- **Terminal tương tác** như PowerShell hoặc terminal trong VS Code để nhập xác nhận và dừng tiến trình bằng `Ctrl+C`.

### Thư viện Python

Các dependency được khai báo trong [requirements.txt](requirements.txt):

| Thư viện | Phiên bản yêu cầu | Vai trò |
| --- | --- | --- |
| `playwright` | `>=1.64.0,<2.0` | Điều khiển Chrome, tải trang và đọc nội dung bài viết |
| `httpx` | `>=0.28.1,<0.29` | Gọi API Ollama ở bước gán nhãn |
| `pydantic` | `>=2.14.1,<3.0` | Kiểm tra cấu hình và cấu trúc dữ liệu |
| `PyYAML` | `>=6.0.3,<7.0` | Đọc cấu hình YAML |
| `portalocker` | `>=4.4.1,<5.0` | Khóa tài nguyên để tránh các tiến trình ghi cùng dataset/profile |

[requirements-dev.txt](requirements-dev.txt) bao gồm các dependency trên và `pytest>=9.1.1,<10.0` để chạy kiểm thử.

**Ollama và model `qwen3:8b`** cần cho `classify`; không cần cho `crawl`, `preprocess` hoặc `reset`. Model chạy trên máy của bạn; thời gian xử lý và bộ nhớ cần thiết phụ thuộc phần cứng, độ dài nội dung và cấu hình context. Các test classification mặc định dùng mock, không cần Ollama đang chạy.

<a id="huong-dan-cai-dat"></a>

## Hướng dẫn cài đặt

Các lệnh dưới đây dùng **PowerShell trên Windows**. Chạy lệnh tại thư mục gốc của repository, nơi có `main.py`.

### 1. Clone repository

```powershell
git clone https://github.com/tukitena/facebook-crawler.git
cd facebook-crawler
```

### 2. Tạo và kích hoạt môi trường ảo

```powershell
python --version
python -m venv .venv
.\.venv\Scripts\Activate.ps1
```

Nếu PowerShell chặn script kích hoạt, có thể dùng trực tiếp Python của môi trường ảo cho mọi lệnh, ví dụ:

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe main.py --help
```

### 3. Cài thư viện và chuẩn bị Chrome

Sau khi kích hoạt môi trường ảo:

```powershell
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
```

Cài **Google Chrome** nếu máy chưa có. Crawler sử dụng Chrome đã cài thông qua `executable_path`, nên không yêu cầu tải thêm Chromium bằng `playwright install` cho luồng chạy này.

Nếu cần phát triển hoặc chạy bộ kiểm thử offline:

```powershell
python -m pip install -r requirements-dev.txt
python -m pytest tests/unit tests/integration -q
```

Các bài kiểm thử trình duyệt vẫn cần Chrome, nhưng sử dụng dữ liệu giả lập, không yêu cầu đăng nhập Facebook.

### 4. Tạo cấu hình cục bộ

Nếu chưa có `config.yaml`, sao chép cấu hình mẫu:

```powershell
Copy-Item config.example.yaml config.yaml
```

Nếu đã có cấu hình riêng, chỉnh sửa file hiện có để giữ các thiết lập của bạn. Kiểm tra danh sách lệnh:

```powershell
python main.py --help
```

### 5. Chuẩn bị Ollama và Qwen để gán nhãn

Cài Ollama theo [hướng dẫn chính thức cho Windows](https://docs.ollama.com/windows). Ứng dụng Ollama trên Windows chạy nền và cung cấp API local; cấu hình mặc định của dự án kết nối tới `http://127.0.0.1:11434`.

Tải model và kiểm tra danh sách model đã cài:

```powershell
ollama pull qwen3:8b
ollama ls
```

Nếu đã có `qwen3:8b`, chỉ cần kiểm tra bằng `ollama ls`; không cần tải lại. Không bắt buộc mở phiên chat bằng `ollama run` trước khi classify. Nếu server chưa chạy, mở ứng dụng Ollama hoặc chạy `ollama serve` trong terminal riêng; không khởi động thêm server khi cổng đã được ứng dụng nền sử dụng. Các lệnh này được mô tả trong [Ollama CLI Reference](https://docs.ollama.com/cli).

Có thể kiểm tra API từ PowerShell:

```powershell
Invoke-RestMethod http://127.0.0.1:11434/api/version
```

Chương trình không tự tải model. Trước khi gán nhãn, nó kiểm tra server, model và thử một câu tổng hợp để xác nhận phản hồi đúng định dạng JSON; câu này không được ghi vào dataset.

<a id="huong-dan-su-dung"></a>

## Hướng dẫn sử dụng

Quy trình: **cấu hình → crawl bài viết → preprocess thành confession → classify → kiểm tra nhãn**. Mỗi bước là một lệnh độc lập:

```powershell
python main.py crawl
python main.py preprocess
python main.py classify --limit 20
```

Nên kiểm tra kết quả của một lượt nhỏ trước khi chạy `python main.py classify` để xử lý tất cả ID còn thiếu nhãn.

<a id="cau-hinh"></a>

### 1. Cấu hình

Tham khảo đầy đủ tại [config.example.yaml](config.example.yaml). Những thiết lập thường cần điều chỉnh trong `config.yaml`:

| Thiết lập | Giá trị trong cấu hình mẫu | Ý nghĩa |
| --- | --- | --- |
| `facebook.page_url` | `https://www.facebook.com/UITconfess` | Trang cần thu thập |
| `chrome.executable_path` | `null` | Tự tìm Chrome; đặt đường dẫn nếu chương trình không tìm được |
| `chrome.user_data_dir` | `runtime/chrome-profile` | Profile riêng lưu phiên đăng nhập |
| `chrome.headless` | `false` | Hiện Chrome để đăng nhập và thao tác bộ lọc |
| `paths.data_dir` | `data/uit` | Thư mục dataset |
| `paths.labeled_dir` | `data/uit/labeled` | Thư mục kết quả gán nhãn; dùng thư mục mới khi đổi thí nghiệm |
| `crawl.max_posts` | `200` | Số **bài viết mới** tối đa được lưu trong một lượt chạy |
| `crawl.max_scrolls` | `200` | Số lần cuộn tối đa trong một lượt chạy |
| `crawl.no_progress_rounds` | `8` | Dừng sau số vòng liên tiếp không lưu thêm bài mới |
| `crawl.scroll_delay_seconds` | `2` | Khoảng chờ giữa các lượt cuộn, tối thiểu 2 giây |
| `preprocess.short_content_chars` | `10` | Gắn cờ xem lại cho confession ngắn hơn ngưỡng này |
| `preprocess.excluded_post_patterns` | `[]` | Các regex Python để loại bài theo nội dung |
| `preprocess.overrides_csv` | `null` | File quyết định nhận/loại bài thủ công, nếu có |
| `ollama.base_url` | `http://127.0.0.1:11434` | API Ollama; chỉ chấp nhận host local `localhost`, `127.0.0.1` hoặc `::1` |
| `ollama.model` | `qwen3:8b` | Tên/tag model phải có trong danh sách model local |
| `ollama.num_ctx` | `8192` | Ngân sách context của model |
| `ollama.num_predict` | `512` | Giới hạn token đầu ra mỗi request |
| `ollama.read_timeout_seconds` | `120` | Thời gian chờ đọc phản hồi; có thể tăng nếu máy xử lý chậm |
| `classification.prompt_path` | `src/classify/prompts/uit-impact-v1.txt` | File rubric dùng để gán nhãn |
| `classification.max_attempts` | `3` | Tổng số lần thử cho mỗi mẫu trong một lượt, cho phép từ 1 đến 3 |
| `classification.review_threshold` | `0.70` | Confidence thấp hơn ngưỡng này được gắn cờ review |
| `classification.max_content_utf8_bytes` | `6000` | Giới hạn byte UTF-8 của nội dung; còn kiểm tra ngân sách tổng prompt |

Ví dụ chỉ định Chrome trên Windows khi gặp `CHROME_NOT_FOUND`:

```yaml
chrome:
  executable_path: 'C:/Program Files/Google/Chrome/Application/chrome.exe'
  user_data_dir: 'runtime/chrome-profile'
  headless: false
```

Đường dẫn tương đối trong YAML được tính từ **thư mục chứa file cấu hình**. Dùng profile riêng cho crawler; không trỏ `user_data_dir` vào profile Chrome cá nhân đang sử dụng. Các đường dẫn raw, processed, labeled và reports phải nằm trong `paths.data_dir` và không chồng lấn nhau.

Để chọn file cấu hình khác, đặt `--config` **trước** tên lệnh:

```powershell
python main.py --config config.yaml crawl
python main.py --config config.yaml preprocess
python main.py --config config.yaml classify --limit 20
```

Khi đổi sang page khác, dùng dataset và các đường dẫn đầu ra riêng cho page đó để tránh trộn nguồn.

<a id="crawl"></a>

### 2. Thu thập dữ liệu — `crawl`

```powershell
python main.py crawl
```

**Đăng nhập Facebook:** Khi chưa có phiên đăng nhập hợp lệ, chương trình mở Chrome bằng profile riêng và chờ bạn đăng nhập trực tiếp trên Facebook, bao gồm các bước xác minh nếu có. Crawler không yêu cầu nhập mật khẩu vào CLI, không đọc hoặc ghi mật khẩu, và không chủ động thu thập thông tin cá nhân của tài khoản dùng để đăng nhập. Chrome vẫn lưu cookie và dữ liệu phiên cục bộ trong profile để dùng cho lần chạy sau; hãy bảo vệ thư mục này và không chia sẻ nó.

Thực hiện theo thứ tự:

1. Đăng nhập Facebook trong cửa sổ Chrome vừa mở và hoàn thành xác minh nếu được yêu cầu.
2. **Đóng các cửa sổ Chrome dùng profile của crawler**, rồi quay lại terminal và **nhấn Enter** để chương trình tiếp tục.
3. Chương trình mở lại Chrome bằng Playwright và tải trang UIT Confessions. Chờ feed hiển thị; lúc này có thể thao tác bộ lọc bài viết trên Facebook.
4. Quay lại terminal, **nhập `y` rồi nhấn Enter để bắt đầu crawl**, hoặc **nhập `n` rồi nhấn Enter để kết thúc**. Input khác, kể cả để trống rồi nhấn Enter, sẽ khiến chương trình hiện lại lời nhắc. Giữ cửa sổ Chrome này mở trong khi crawler chạy; chọn `n` sẽ đóng cửa sổ này và kết thúc bình thường.

Nếu profile còn phiên đăng nhập hợp lệ, có thể không cần bước đăng nhập thủ công; bạn vẫn cần xác nhận bắt đầu crawl sau khi trang tải xong.

Có thể thay giới hạn cho một lượt chạy:

```powershell
python main.py crawl --max-posts 500 --max-scrolls 500
```

Crawler đọc nội dung bài viết, mở phần “Xem thêm/See more” khi nhận diện được và bỏ qua nội dung đã lưu. Mặc định, ảnh, video và font bị chặn để giảm tải; nội dung chỉ nằm trong ảnh/video không được trích xuất bằng OCR.

Terminal hiển thị số bài đã lưu, số bài trùng và tiến độ cuộn. `candidate_segments` chỉ là số marker được đếm sơ bộ; số confession chính thức được xác định sau `preprocess`. `crawled_at` là thời điểm thu thập, không phải ngày đăng bài.

Nhấn **Ctrl+C** để dừng. Các bản ghi đã ghi hoàn tất được giữ lại; chạy lại cùng lệnh và cấu hình để tiếp tục thu thập. Crawler không bảo đảm lấy được toàn bộ lịch sử page vì dữ liệu phụ thuộc feed Facebook hiển thị cho tài khoản.

<a id="tiep-tuc-crawl"></a>

### 3. Tiếp tục khi tốc độ crawl giảm

Nếu Chrome tải chậm sau nhiều lượt cuộn, có thể khởi động lại tiến trình:

1. Ghi nhớ **ngày đăng của bài cuối cùng đã được crawl** trên giao diện Chrome.
2. Nhấn **Ctrl+C** trong terminal để dừng tiến trình hiện tại.
3. Chạy lại `python main.py crawl` với cùng file cấu hình.
4. Khi trang UIT Confessions mở lại, **chưa nhập `y` để bắt đầu crawl**. Dùng bộ lọc bài viết trên Facebook, nếu có, để chọn mốc ngày hoặc khoảng thời gian tương ứng với bài đã ghi nhớ và phần lịch sử muốn thu thập tiếp.
5. Chờ kết quả lọc tải xong, quay lại terminal và **nhập `y` rồi nhấn Enter** để bắt đầu lượt mới; nhập `n` nếu muốn kết thúc.

Bộ lọc giúp giảm việc duyệt lại bài cũ; cơ chế loại trùng vẫn bỏ qua nội dung đã lưu nếu có phần chồng lặp. Chương trình chưa lưu vị trí cuộn hoặc tự khôi phục bộ lọc theo ngày. Không cần chạy `reset` cho thao tác này.

<a id="preprocess"></a>

### 4. Tiền xử lý — `preprocess`

Sau khi crawl, chạy:

```powershell
python main.py preprocess
```

Lệnh chạy offline, không mở Chrome hoặc gọi Ollama. Đầu vào gồm `raw_posts.csv` và metadata đi kèm `raw_posts.meta.json` do crawler tạo. Mỗi lượt đọc toàn bộ dữ liệu raw, tạo lại bộ output processed và giữ nguyên raw.

Các quy tắc chính:

- Tách confession khi gặp marker số ở đầu dòng, chẳng hạn `#001`, `#1 Nội dung` hoặc `#1: Nội dung`. Marker nằm giữa câu và hashtag chữ không mở confession mới.
- Kết thúc đoạn khi gặp marker tiếp theo, dòng chỉ gồm từ hai dấu `-` trở lên hoặc cuối bài.
- Gỡ nhãn giao diện `See less`/`Ẩn bớt` và footer `#UITconfessions` (có thể kèm `:`) ở đuôi bài. Cũng gỡ đúng dòng cuối `#UITconfessions: bit.ly/UITConfessions` khi dòng đứng riêng và khớp chính xác. Các cụm từ nằm giữa lời confession được giữ lại.
- Loại bài không có marker và ghi lý do `NO_MARKER`; đoạn rỗng hoặc vùng bị loại được ghi vào `excluded.csv` để đối chiếu.
- Gộp confession có nội dung trùng sau chuẩn hóa khoảng trắng. Khác từ ngữ, hoa/thường hoặc dấu vẫn được giữ riêng.
- Giữ `confession_number` từ marker, bao gồm số 0 đầu. Số này có thể trùng và không phải khóa chính; ID được tạo từ nội dung. Khi gộp nội dung trùng, giữ number của lần gặp đầu và lưu mọi nguồn trong `sources.csv`.

Ví dụ đầu vào tổng hợp:

```text
#001
Mình muốn hỏi giờ mở cửa thư viện UIT.
----
#002 Cảm ơn nhà trường đã hỗ trợ hoạt động học thuật.
#UITconfessions
```

Kết quả là hai confession riêng, có `confession_number` lần lượt là `001` và `002`; marker, dòng phân cách và footer không nằm trong nội dung đầu ra.

**Điều chỉnh bộ lọc:** Có thể thêm regex vào `preprocess.excluded_post_patterns`, hoặc đặt `preprocess.overrides_csv` trỏ tới CSV có header `post_id,decision,note`. Mỗi `post_id` lấy từ raw xuất hiện tối đa một lần; `decision` nhận `accept` hoặc `reject`. `accept` bỏ qua bộ lọc loại bài nhưng vẫn yêu cầu marker hợp lệ; `reject` loại cả bài. ID không tồn tại hoặc regex sai khiến lệnh dừng để bạn sửa đầu vào.

Các cờ `SHORT_CONTENT`, `REPEATED_MARKER` và `MANUAL_ACCEPT` trong `sources.csv` đánh dấu mẫu cần xem lại; chúng không tự loại confession. Khi nhập CSV vào Excel, chọn kiểu **Text** cho `confession_number` và `marker` để giữ số 0 đầu.

Có thể chạy lại `preprocess` sau khi crawl thêm hoặc đổi bộ lọc, không cần reset. Nếu bộ nhãn cũ không tương thích, chương trình dừng với `DERIVED_DATA_CONFLICT`; hãy lưu trữ bộ nhãn cũ hoặc chọn `paths.labeled_dir` mới trước khi chạy lại.

<a id="classify"></a>

### 5. Gán nhãn sentiment — `classify`

Sau khi `preprocess` thành công và Ollama đang chạy, thử một lượt nhỏ:

```powershell
python main.py classify --limit 20
```

Lệnh xác minh bộ processed, đọc từng confession và gửi riêng nội dung tới Qwen. Model trả nhãn, confidence và giải thích ngắn; chương trình kiểm tra phản hồi trước khi ghi CSV. ID và nội dung trong kết quả lấy nguyên từ input, không để model viết lại. Bước này không mở Chrome và không truy cập Facebook.

`--limit N` nhận số nguyên dương và giới hạn **N ID chưa có nhãn được thử trong lượt này**, gồm cả mẫu kết thúc bằng failure. Retry cùng một ID không tăng số ID đã thử. Còn pending sau khi hết limit thì trạng thái là `partial`, exit code `3`; đây là kết quả dự kiến khi chạy thử một phần dataset.

Sau khi kiểm tra kết quả, chạy tiếp tất cả mẫu còn thiếu nhãn:

```powershell
python main.py classify
```

**Tiến trình và chạy lại:** Terminal hiển thị `total`, `already_done`, `attempted`, `newly_saved`, `failed`, `remaining` và tỷ lệ review của các nhãn mới. `remaining` bao gồm ID bị lỗi. Nhấn `Ctrl+C` để dừng; các nhãn đã ghi hoàn tất được giữ lại. Chạy lại cùng cấu hình để bỏ qua ID đã lưu và thử lại ID còn thiếu, kể cả ID từng lỗi. Chạy `--limit 20` lần hai sẽ thử tối đa 20 ID pending tiếp theo, không chạy lại 20 nhãn đã thành công.

Nhãn được xác định theo **đánh giá về UIT**, không chỉ theo cảm xúc chung của người viết:

| Nhãn | Ý nghĩa | Ví dụ tổng hợp |
| --- | --- | --- |
| `Positive` | Đánh giá tích cực về UIT hoặc hoạt động, dịch vụ của trường | “UIT tổ chức buổi tư vấn học tập rất hữu ích.” |
| `Negative` | Phàn nàn hoặc đánh giá tiêu cực về UIT hoặc hoạt động, dịch vụ của trường | “Thiết bị phòng học của UIT hỏng lâu nhưng chưa được sửa.” |
| `Neutral` | Hỏi thông tin, không có đánh giá rõ về UIT, hoặc nội dung không liên quan tới trường | “Cho mình hỏi lịch đăng ký môn học ở UIT.” |

Một confession buồn về chuyện cá nhân không mặc nhiên là `Negative` đối với UIT. Khen/chê page confession hoặc admin page không tự động là đánh giá về trường. Mẫu vừa khen vừa chê chọn hướng đánh giá trội; nếu cân bằng thì `Neutral` và cần review. Nhãn phản ánh nội dung người viết thể hiện, không xác nhận các nhận định trong bài là sự thật; `confidence` của model cũng không phải độ chính xác đã được kiểm chứng.

**Kiểm tra nhãn:** `needs_review=true` khi confidence dưới ngưỡng, model không chắc đối tượng, nội dung mixed, model tự yêu cầu review hoặc nguồn mang cờ cần xem lại. Cột `review_reasons` lưu các mã phân cách bằng `;`:

| Mã | Ý nghĩa |
| --- | --- |
| `LOW_CONFIDENCE` | Confidence thấp hơn `classification.review_threshold` |
| `UNCERTAIN_TARGET` | Chưa chắc nội dung đánh giá UIT |
| `MIXED_IMPACT` | Nội dung vừa tích cực vừa tiêu cực |
| `MODEL_UNCERTAIN` | Model tự yêu cầu kiểm tra |
| `SHORT_CONTENT`, `REPEATED_MARKER`, `MANUAL_ACCEPT` | Cờ được chuyển từ provenance sau preprocess |

Nhãn cần review vẫn được lưu. Nên kiểm tra toàn bộ nhóm này và lấy thêm mẫu từ nhóm không bị cờ; confidence cao không bảo đảm đúng. Lưu sửa nhãn thủ công vào file riêng, giữ ID để đối chiếu, không sửa trực tiếp `labeled.csv` đang dùng để resume hoặc các file processed. Chất lượng cần được đo bằng tập do người gán nhãn độc lập; việc chạy thành công không tự chứng minh accuracy.

**Lỗi từng mẫu:** Lỗi tạm thời hoặc phản hồi sai định dạng được thử lại trong ngân sách tối đa ba lần/mẫu. Mẫu vẫn thất bại được ghi vào `classification_failures.csv`; chương trình không gán `Neutral` thay cho lỗi. Nội dung quá dài được ghi `INPUT_TOO_LONG`, không bị cắt; giới hạn còn tính tổng prompt nên body dưới 6000 byte vẫn có thể vượt ngân sách. Model thiếu, cấu hình API không tương thích hoặc dịch vụ hỏng liên tiếp có thể khiến lệnh dừng sớm.

**Không trộn bộ nhãn:** `labeled.meta.json` lưu fingerprint của input, model/tag/digest, phiên bản Ollama, prompt/schema và các cấu hình ảnh hưởng kết quả. Nếu chúng thay đổi, chương trình dừng với `RESUME_MISMATCH` thay vì ghép nhãn khác thí nghiệm. Đổi riêng timeout hoặc `--limit` không đổi fingerprint. Để thử prompt/model/context khác, chọn thư mục kết quả mới trong config:

```yaml
paths:
  labeled_dir: data/uit/labeled-experiment-02
```

Chỉ cập nhật khóa này trong phần `paths` hiện có; không thêm một phần `paths` trùng tên. Thư mục mới vẫn phải nằm trong `paths.data_dir` và không chồng lấn các output khác. Không xóa metadata hoặc dùng `reset` chỉ để vượt lỗi fingerprint: reset xóa cả raw và processed của cấu hình đó.

<a id="du-lieu"></a>

### 6. Dữ liệu đầu ra

Với cấu hình mặc định:

| Đường dẫn | Nội dung |
| --- | --- |
| `data/uit/raw/raw_posts.csv` | Bài viết thô: `id`, `content`, `crawled_at`, `source_url`, `post_kind` |
| `data/uit/raw/raw_posts.meta.json` | Metadata của nguồn raw và quy tắc định danh |
| `data/uit/processed/confessions.csv` | Confession duy nhất: `id`, `content`, `confession_number` |
| `data/uit/processed/sources.csv` | Nguồn của từng lần xuất hiện: bài raw, marker, vị trí dòng, URL và cờ kiểm tra |
| `data/uit/processed/excluded.csv` | Bài hoặc vùng bị loại, lý do và trích đoạn tối đa 200 ký tự |
| `data/uit/processed/preprocess.manifest.json` | Hash, phiên bản xử lý, thống kê và ID của bộ output |
| `data/uit/labeled/labeled.csv` | Nhãn: `id`, `content`, `label`, `confidence`, `reason`, `needs_review`, `review_reasons` |
| `data/uit/labeled/classification_failures.csv` | Lỗi cuối của từng ID chưa thành công: `id`, `error_code`, `attempts`, `last_attempt_at` |
| `data/uit/labeled/labeled.meta.json` | Fingerprint và metadata của bộ nhãn để kiểm tra resume |
| `data/uit/reports/<run_id>.json` | Báo cáo từng lượt crawl/preprocess/classify, trạng thái và thống kê |
| `logs/crawl.jsonl` | Log có cấu trúc để chẩn đoán lỗi |
| `runtime/chrome-profile/` | Profile Chrome cục bộ và phiên đăng nhập |

CSV dùng UTF-8 BOM để thuận tiện đọc tiếng Việt. Dữ liệu raw, processed và trích đoạn trong `excluded.csv` là dữ liệu nội dung; log/report vận hành không ghi body bài viết hoặc mật khẩu. Dataset, profile, cấu hình cục bộ và log được bỏ qua bởi `.gitignore`.

`confession_number` không có trong `labeled.csv`; đối chiếu bằng `id` với `processed/confessions.csv`. Nhãn giữ thứ tự ghi thành công, có thể khác thứ tự input sau khi retry/resume. `attempts` trong failure là số request của lần xử lý gần nhất; `0` nghĩa là mẫu bị chặn trước khi gọi model, chẳng hạn do quá dài. Failure cũ của ID đã thành công được loại khi đối soát.

Khi tự viết script đọc output processed, dùng `src.preprocess.storage.validate_generation(processed_dir)` để xác minh bộ dữ liệu và giữ dataset lock trong suốt lượt đọc. Nếu tiến trình bị đóng cưỡng bức giữa lúc thay các file output, chạy lại `preprocess` từ raw để tạo bộ nhất quán. Phiên bản splitter hiện tại là `line_start_numeric_v3`; output từ phiên bản cũ cần được tạo lại.

<a id="reset"></a>

### 7. Xóa dataset để thu thập lại — `reset`

**Lệnh này xóa dữ liệu ngay, không có bước xác nhận.** Sao lưu dataset cần giữ trước khi chạy:

```powershell
python main.py reset
```

Hoặc chọn đúng cấu hình của dataset cần xóa:

```powershell
python main.py --config config.yaml reset
```

Lệnh xóa raw, metadata, bộ output preprocessing và các file gán nhãn do chương trình quản lý (`labeled.csv`, `classification_failures.csv`, `labeled.meta.json`) nếu tồn tại. Chrome profile/phiên đăng nhập, cấu hình, log, báo cáo lịch sử và file thủ công như overrides được giữ lại.

Reset không tự khởi động crawler. Sau đó chạy `python main.py crawl` với cùng cấu hình để thu thập lại từ feed hiện tại. Không cần reset nếu chỉ muốn tiếp tục crawl hoặc chạy lại preprocess.

<a id="cau-hoi-thuong-gap"></a>

## Câu hỏi thường gặp

### Không tìm thấy cấu hình hoặc cấu hình không hợp lệ?

Với `CONFIG_ERROR`, kiểm tra đã tạo `config.yaml` từ bản mẫu, đúng cú pháp YAML và đường dẫn chưa. Tên khóa lạ hoặc sai kiểu dữ liệu sẽ bị từ chối. Đặt `--config` trước tên lệnh, ví dụ `python main.py --config config.yaml crawl`.

### Không mở được Chrome hoặc profile đang được sử dụng?

Với `CHROME_NOT_FOUND`, cài Chrome hoặc đặt đúng `chrome.executable_path`. Với `PROFILE_IN_USE`, đóng các cửa sổ Chrome dùng profile của crawler rồi làm theo yêu cầu trên terminal. Không dùng chung profile này cho hai tiến trình.

### Đã đăng nhập nhưng chương trình vẫn chờ hoặc yêu cầu đăng nhập lại?

Phải đóng cửa sổ đăng nhập thủ công trước lần nhấn Enter đầu tiên. Khi Chrome do crawler mở lại và trang tải xong, nhập `y` rồi nhấn Enter để bắt đầu, hoặc nhập `n` rồi nhấn Enter để kết thúc. Chỉ nhấn Enter hoặc nhập giá trị khác sẽ hiện lại lời nhắc. Nếu phiên hết hạn hoặc xuất hiện yêu cầu xác minh, xử lý trực tiếp trên Facebook rồi chạy lại. Dùng terminal tương tác; crawler cần nhận được xác nhận từ bàn phím.

### Trang tải lỗi, không nhận diện được bài hoặc crawl dừng sớm?

Kiểm tra kết nối, URL và khả năng truy cập page của tài khoản. `NAVIGATION_TIMEOUT` liên quan đến tải trang; `PAGE_UNAVAILABLE` hoặc `UNSUPPORTED_LAYOUT` có thể do trang chưa sẵn sàng hoặc giao diện Facebook thay đổi. Xem log/report để xác định nguyên nhân; nếu cấu trúc giao diện thay đổi, adapter đọc bài cần được cập nhật.

Crawler cũng dừng khi đạt giới hạn bài/lượt cuộn hoặc nhiều vòng không có bài mới. Vì vậy, trạng thái hoàn tất một lượt không có nghĩa đã thu thập hết page. Nếu tốc độ giảm, thực hiện [quy trình khởi động lại và lọc theo ngày](#tiep-tuc-crawl).

### Chạy lại crawl có lưu bài trùng không? Số bài có bằng số confession không?

Crawler nhận diện bài đã lưu theo nội dung chuẩn hóa. Bài giữ nguyên nội dung được bỏ qua; bài bị chỉnh sửa có thể tạo bản ghi khác. Một bài có thể chứa nhiều confession hoặc không có confession hợp lệ, nên số dòng raw và số dòng processed thường khác nhau.

### Preprocess không tạo confession cho một số bài?

Xem `excluded.csv` để biết lý do. Bài không có marker số đầu dòng được ghi `NO_MARKER`; bộ lọc hoặc override cũng có thể loại bài. Nội dung chỉ nằm trong ảnh/video không được crawler chuyển thành văn bản. Các confession chỉ khác khoảng trắng có thể đã được gộp; xem `sources.csv` để truy nguồn.

### File bị khóa hoặc dữ liệu dẫn xuất không nhất quán?

- `RESOURCE_LOCKED`: Đợi tiến trình đang giữ dataset/profile kết thúc; mỗi tài nguyên chỉ nên có một tiến trình sử dụng tại một thời điểm.
- `OUTPUT_LOCKED`: Đóng CSV đang mở trong Excel hoặc ứng dụng khác rồi chạy lại.
- `DERIVED_DATA_CONFLICT`: Lưu trữ bộ nhãn cũ hoặc cấu hình `labeled_dir` mới trước khi tạo lại processed.
- `RESUME_MISMATCH` khi classify: kiểm tra input, prompt, model, phiên bản Ollama và metadata; dùng `labeled_dir` mới nếu đã đổi thí nghiệm, không tự xóa metadata để tiếp tục.
- Output processed không hợp lệ sau khi tiến trình bị đóng cưỡng bức: chạy lại `preprocess` từ raw. Với `Ctrl+C` trong lúc ghi output, xem trường `published` của report để biết bộ mới đã được ghi xong chưa.
- CSV nhãn bị cắt hoặc hỏng: giữ lại file để kiểm tra, khôi phục từ bản sao lưu hoặc dùng output mới; chương trình không tự bỏ dòng hỏng hay sửa CSV để resume.
- Reset bị ngắt hoặc xóa chưa hết: xử lý lỗi quyền ghi/file bị khóa rồi chạy lại `reset` để hoàn tất trước khi crawl.

### Không kết nối được Ollama hoặc không tìm thấy model?

Kiểm tra Ollama đang chạy, `ollama.base_url` đúng endpoint local và `ollama ls` có đúng tag trong `ollama.model`. Với `MODEL_NOT_FOUND`, tải model bằng `ollama pull qwen3:8b` hoặc sửa cấu hình cho đúng model đã cài. Với `MODEL_UNAVAILABLE`, kiểm tra server và tài nguyên máy rồi chạy lại; nhãn đã lưu vẫn được giữ.

`MODEL_PREFLIGHT_FAILED` hoặc `MODEL_REQUEST_FAILED` có thể do server/model không đáp ứng JSON schema hay options đã cấu hình. Kiểm tra log và cấu hình; chương trình không tự đổi model hoặc bỏ options. Nếu đổi model hay nâng phiên bản Ollama, fingerprint có thể thay đổi và cần thư mục nhãn mới.

### Vì sao classify trả `partial` hoặc vẫn còn failure khi chạy lại?

`partial`/exit `3` nghĩa là còn ID thiếu nhãn, do đạt limit hoặc có failure. Xem `classification_failures.csv`; các mẫu lỗi vẫn được thử lại ở lượt sau. Với `INPUT_TOO_LONG`, chạy lại cùng giới hạn không làm nội dung ngắn hơn: giữ mẫu để xử lý riêng, hoặc thử context/giới hạn phù hợp trong một thư mục nhãn mới sau khi kiểm tra khả năng của model. Không sửa metadata để ghép kết quả khác cấu hình.

### Có cần mở Chrome hoặc Internet để classify không?

Không cần Chrome hay Facebook cho classify. Sau khi đã tải model và có bộ processed hợp lệ, chương trình gọi Ollama local; tải model ban đầu vẫn cần kết nối mạng. `classify` không tự crawl hoặc preprocess nếu thiếu input.

### Mã thoát của chương trình có ý nghĩa gì?

| Mã | Ý nghĩa |
| --- | --- |
| `0` | Lệnh hoàn tất thành công hoặc người dùng chọn `n` để kết thúc trước khi crawl |
| `1` | Lỗi I/O hoặc runtime |
| `2` | Lỗi đầu vào, cấu hình, khóa tài nguyên hoặc xung đột dữ liệu |
| `3` | Hoàn tất một phần: crawl còn lỗi/mất phiên, hoặc classify còn ID thiếu nhãn do limit/failure |
| `130` | Người dùng dừng bằng `Ctrl+C` |

<a id="cam-ket-va-mien-tru"></a>

## Cam kết và miễn trừ

### Mục đích sử dụng

Công cụ được xây dựng cho mục đích **học tập và nghiên cứu** trong phạm vi bài tập CS232.R11. Không sử dụng công cụ để thu thập dữ liệu cá nhân, theo dõi cá nhân, gửi thư rác hoặc thực hiện hành vi xâm phạm quyền riêng tư. Dự án không đại diện cho Facebook/Meta hoặc đơn vị quản lý trang UIT Confessions.

### Tài khoản và quyền riêng tư

Người dùng tự đăng nhập trên Facebook. Mã crawler không thu thập, lưu trữ hoặc gửi mật khẩu cho tác giả dự án, và không được thiết kế để trích xuất hồ sơ cá nhân của tài khoản đăng nhập. Cookie và phiên đăng nhập được Chrome lưu cục bộ trong profile riêng; không đưa profile, cookie hoặc thông tin đăng nhập lên repository hay chia sẻ cho người khác.

Nội dung confession vẫn có thể chứa thông tin cá nhân do người đăng viết vào bài. Công cụ hiện chưa có bước tự động ẩn danh toàn bộ nội dung; người sử dụng cần rà soát và loại bỏ hoặc ẩn danh thông tin đó trước khi sử dụng, nộp bài hay chia sẻ dataset.

### Trách nhiệm của người sử dụng

Người sử dụng có trách nhiệm tuân thủ quyền riêng tư, quyền đối với nội dung, quy định pháp luật áp dụng và điều khoản dịch vụ của Facebook. Chỉ truy cập dữ liệu mà tài khoản được phép xem; không dùng công cụ để vượt qua hạn chế truy cập hoặc cơ chế xác minh của nền tảng.

### Miễn trừ trách nhiệm

Công cụ được cung cấp phục vụ học tập, không bảo đảm hoạt động liên tục, thu thập đầy đủ dữ liệu hoặc tạo nhãn chính xác tuyệt đối. Facebook có thể thay đổi giao diện, giới hạn truy cập hoặc yêu cầu xác minh trong quá trình sử dụng.

Trong phạm vi pháp luật cho phép, tác giả không chịu trách nhiệm đối với việc tài khoản Facebook bị hạn chế hoặc khóa, mất dữ liệu, thiệt hại phát sinh hay vấn đề pháp lý do cách người dùng vận hành công cụ. Người dùng tự cân nhắc và chịu trách nhiệm về việc sử dụng cũng như xử lý, lưu trữ và chia sẻ dữ liệu thu thập được.
