# Zalo Relocator

Ứng dụng Windows giúp di chuyển dữ liệu Zalo Desktop khỏi ổ C sang ổ đĩa khác, sau đó tạo NTFS junction để Zalo tiếp tục sử dụng đường dẫn cũ như bình thường.

> Zalo Relocator là dự án clean-room độc lập, không chứa hoặc sử dụng mã nguồn của Zalo hay ZaloMove.

## Tính năng

- Giao diện Tkinter nhẹ, không cần cài Python khi sử dụng bản `.exe`.
- Tự nhận diện các thư mục Zalo phổ biến:
  - `%LocalAppData%\Programs\Zalo`
  - `%LocalAppData%\ZaloPC` — dữ liệu của một số phiên bản cũ
  - `%AppData%\ZaloData`
  - `%USERPROFILE%\Documents\Zalo Received Files`
  - `%LocalAppData%\zalo-updater`
- **Chế độ an toàn:** copy dữ liệu sang đích, đổi thư mục gốc thành `-Old`, rồi tạo junction.
- **Chế độ move:** chuyển dữ liệu sang đích và tạo junction, không giữ backup `-Old`.
- **Tạo lại liên kết:** sử dụng khi dữ liệu đã có trên ổ đĩa khác, chẳng hạn sau khi cài lại Windows.
- Copy đa luồng với tối đa 8 I/O worker, phù hợp với bộ dữ liệu lớn.
- Progress bar hiển thị phần trăm, dung lượng, tốc độ và thời gian dự kiến còn lại.
- Nhận diện Zalo đang chạy theo tên process và đường dẫn file thực thi.
- Khôi phục dữ liệu từ backup `-Old`.
- Xoá backup `-Old` trực tiếp trong ứng dụng.
- Tự mở Zalo sau khi di chuyển để người dùng kiểm tra.
- Giữ hộp thoại xác nhận cho đến khi người dùng chọn xoá hoặc giữ backup.
- Dọn các thư mục phiên bản Zalo cũ dạng `Zalo-x.y.z`, luôn giữ lại phiên bản mới nhất.
- Ghi nhật ký tiến trình theo thời gian thực.

## Yêu cầu

- Windows 10 hoặc Windows 11, 64-bit.
- Nên chạy bằng **Run as administrator** để tạo junction ổn định.
- Ổ đĩa đích nên dùng NTFS.
- Phải thoát hoàn toàn Zalo trước khi di chuyển, khôi phục hoặc dọn phiên bản cũ.

Người sử dụng file `.exe` không cần cài Python hay Tkinter.

## Sử dụng bản EXE

1. Tải `ZaloRelocator.exe` từ mục Releases.
2. Thoát hoàn toàn Zalo, kể cả biểu tượng Zalo trong khay hệ thống.
3. Nhấp chuột phải vào file và chọn **Run as administrator**.
4. Chọn thư mục đích, ví dụ `D:\Zalo`.
5. Giữ **Chế độ an toàn** trong lần di chuyển đầu tiên.
6. Chọn các thư mục cần xử lý và bấm **TỰ ĐỘNG DI CHUYỂN**.
7. Khi Zalo được mở lại, kiểm tra đăng nhập, tin nhắn, hình ảnh và việc gửi/nhận file.
8. Chỉ xác nhận xoá `-Old` khi chắc chắn Zalo hoạt động bình thường; nếu chưa chắc, chọn giữ lại.

## Chạy từ mã nguồn

Yêu cầu Python 3.9 trở lên:

```powershell
git clone <repository-url>
cd ZaloRelocator
python .\src\main.py
```

## Build file EXE

Chạy script build có sẵn:

```powershell
powershell -ExecutionPolicy Bypass -File .\build.ps1
```

Script sẽ cài PyInstaller, nhúng icon ứng dụng và tạo:

```text
dist\ZaloRelocator.exe
```

## Các chế độ hoạt động

### An toàn — khuyên dùng

```text
Copy sang đích → đổi thư mục gốc thành -Old → tạo junction → mở Zalo kiểm tra
```

Nếu Zalo chưa hoạt động ổn định, có thể dùng chức năng **Khôi phục từ -Old**.

### Move

```text
Move sang đích → tạo junction → mở Zalo kiểm tra
```

Chế độ này tiết kiệm dung lượng ngay nhưng không tạo backup tại đường dẫn cũ.

### Tạo lại liên kết

Dùng khi dữ liệu đã tồn tại tại thư mục đích. Công cụ sẽ tạo lại junction tại đường dẫn Zalo mặc định mà không copy dữ liệu lần nữa.

## Cấu trúc project

```text
ZaloRelocator/
├─ assets/
│  ├─ ZaloRelocator.ico
│  └─ ZaloRelocator.png
├─ src/
│  └─ main.py
├─ build.ps1
├─ LICENSE
└─ README.md
```

## Lưu ý an toàn

- Luôn sao lưu dữ liệu quan trọng trước khi thao tác.
- Không tắt máy hoặc kết thúc ứng dụng khi đang copy/move.
- Không xoá `-Old` trước khi kiểm tra Zalo hoạt động ổn định.
- Không chạy file `Uninstall Zalo.exe` bên trong thư mục phiên bản cũ; hãy dùng chức năng dọn phiên bản của công cụ.
- Tốc độ thực tế phụ thuộc vào ổ nguồn, ổ đích, số lượng file nhỏ và phần mềm antivirus.

## Giấy phép

Dự án được phát hành theo giấy phép [MIT](LICENSE).

## Tuyên bố

Zalo là sản phẩm và nhãn hiệu của đơn vị sở hữu tương ứng. Dự án này không liên kết, không được tài trợ và không được xác nhận bởi Zalo. Người dùng tự chịu trách nhiệm sao lưu và kiểm tra dữ liệu của mình.
