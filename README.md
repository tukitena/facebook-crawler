# Facebook confession crawler

Thu thập bài từ một Facebook page bằng Chrome profile riêng, sau đó tách dữ liệu raw thành các confession để chuẩn bị gán nhãn. Các lệnh dùng Python và dependency trong `requirements.txt`. Sao chép `config.example.yaml` thành `config.yaml`, điều chỉnh page và đường dẫn trước khi chạy.

```powershell
python main.py crawl
python main.py preprocess
python main.py --config config.yaml preprocess
```

`--config` đặt trước tên lệnh. Đường dẫn trong YAML được tính từ thư mục chứa config. Preprocess chạy offline, không mở Chrome hoặc gọi Ollama; cần `raw_posts.csv` và `raw_posts.meta.json` do crawler tạo.

## Output preprocessing

Mỗi lần chạy đọc toàn bộ raw, giữ nguyên raw và tạo bộ output trong `paths.processed_dir`:

| File | Nội dung |
| --- | --- |
| `confessions.csv` | `id,content,confession_number`: mỗi nội dung duy nhất một dòng |
| `sources.csv` | Nguồn raw, marker, dòng bắt đầu/kết thúc, URL và cờ kiểm tra của mỗi lần xuất hiện |
| `excluded.csv` | Bài/vùng bị loại, lý do và excerpt tối đa 200 ký tự |
| `preprocess.manifest.json` | Hash, phiên bản xử lý, counts và ID của bộ output |

CSV dùng UTF-8 BOM. Khi nhập CSV vào bảng tính, chọn kiểu **Text** cho `confession_number` và `marker` để giữ số 0 đầu. Number là mã hiển thị của bài, có thể trùng; `id` bắt đầu `cfs_` được tính từ nội dung đã chuẩn hóa. Nội dung chỉ khác khoảng trắng được gộp, giữ cách hiển thị và number của lần gặp đầu; mọi lần xuất hiện vẫn có trong `sources.csv`.

Marker phải ở đầu dòng, dạng `#001`, `#1 Nội dung` hoặc `#1: Nội dung`. Hashtag chữ và marker giữa câu không mở confession. Dòng chỉ gồm từ hai dấu `-` trở lên kết thúc đoạn. Footer `#UITconfessions` (có thể có `:`) và nhãn `See less`/`Ẩn bớt` chỉ được gỡ ở cuối bài. Các quy tắc có phân biệt hoa/thường; bài không có marker được ghi `NO_MARKER`.

Phiên bản `line_start_numeric_v3` còn gỡ đúng dòng cuối `#UITconfessions: bit.ly/UITConfessions` sau khi gỡ nhãn UI. Dòng này phải đứng riêng và khớp chính xác; link khác hoặc cụm từ nằm trong câu được giữ. Output phiên bản cũ cần chạy lại preprocess; bộ nhãn gắn với generation cũ vẫn chịu kiểm tra conflict.

Cờ trong sources: `SHORT_CONTENT` giữ đoạn ngắn để xem lại; `REPEATED_MARKER` cho number lặp trong cùng bài; `MANUAL_ACCEPT` cho bài nhận theo override. Các cờ không tự loại nội dung. Dòng nguồn tính từ 1 trong raw.

## Điều chỉnh bộ lọc

Block `preprocess` trong config mẫu cho phép thêm `excluded_post_patterns` (regex Python, multiline) và `overrides_csv`. File override có header `post_id,decision,note`, mỗi ID raw xuất hiện tối đa một lần, decision là `accept` hoặc `reject`. `accept` bỏ qua bộ lọc loại bài/regex nhưng vẫn yêu cầu marker; `reject` loại cả bài. ID không có trong raw và regex không hợp lệ làm lệnh dừng trước khi publish.

## Chạy lại và khôi phục

Chạy lại `preprocess` để rebuild. Cùng raw, config, override và phiên bản xử lý cho ba CSV giống byte; generation ID và thời gian report thay đổi. Tiến độ hiển thị trên stderr, report nằm tại `paths.reports_dir/<run_id>.json`, log JSONL nằm tại `paths.log_dir/crawl.jsonl`. Log/report không chứa body, excerpt hoặc note override.

Chỉ một lệnh được giữ khóa dataset tại một thời điểm. Nếu gặp `RESOURCE_LOCKED`, đợi lệnh đang chạy kết thúc. Với `OUTPUT_LOCKED`, đóng file CSV đang mở trong Excel rồi chạy lại; file tạm có thể được giữ để kiểm tra.

Manifest được ghi cuối sau khi thay ba CSV. Khi process bị kill hoặc lỗi I/O giữa các lần thay, output có thể chưa nhất quán. Consumer phải dùng `src.preprocess.storage.validate_generation(processed_dir)` trước khi đọc và giữ dataset lock trong suốt lượt đọc. Nếu validation thất bại, chạy lại preprocess từ raw. Ctrl+C trước publish giữ output cũ; Ctrl+C trong publish được xử lý sau khi ghi xong, xem `published` trong report.

Nếu `labeled.csv` đã có record và input gán nhãn thay đổi hoặc không xác minh được, lệnh dừng với `DERIVED_DATA_CONFLICT`. Lưu trữ bộ nhãn cũ hoặc chọn `labeled_dir` mới trước khi chạy lại. Lỗi ghi report sau publish được báo riêng; generation đã publish vẫn còn hợp lệ.

`python main.py reset` xóa raw, metadata và các output được chương trình quản lý để crawl lại; giữ profile, config, log, report và file thủ công như overrides. Chỉ dùng reset khi muốn xóa dataset, không cần reset để chạy lại preprocess. Exit code: thành công `0`, lỗi I/O/runtime `1`, input/config/lock/conflict `2`, Ctrl+C `130`.
