# Vendor files for the ATORCH BW600 / BW600-DK

These are all the files ATORCH publishes for the BW600 and the BW600-DK. They were downloaded on 2026-09-23 and are
stored **unmodified**, as a reference and in case the vendor pages change or disappear. They're © ATORCH (ATorch
Innovative Manufactory Co., Ltd.) and are not covered by this project's code.

Both models report themselves over USB as `ATORCH BW600 V2.0.5`, and they run the same firmware and PC software. The
BW600-DK is the WiFi/Bluetooth (Tuya) version, made in 150 / 300 / 450 / 600 W variants.

## From [BW600 User Manual and PC software](http://en.atorch.cn/NewsDetail.aspx?ID=92)

| File | Contents | Uploaded | Bytes |
|------|----------|----------|------:|
| `02-BW150_PC_Application_V1.0.8.zip` | Windows PC software V1.0.8 (Qt), including the firmware updater. | 2025-09-04 | 22,429,395 |
| `User Manual for Electronic Load Software.pdf` | Manual for the PC software (DL24/DL24P/BW150/BW600/BW600-DK). | 2025-08-30 | 6,687,805 |
| `BW600-6321-APP-2-0-3.zip` | Firmware V2.0.3. | 2025-07-30 | 683,620 |
| `BW600-APP-2-0-5(定制取消长按+和-).zip` | Firmware V2.0.5, customised build: long-press on + and − disabled (定制取消长按+和-). | 2025-12-15 | 691,016 |
| `BW600-6321-APP-2-0-5-UP-2026.07.08.zip` | Firmware V2.0.5, standard build. | 2026-07-08 | 690,802 |

## From [BW600-DK Series User Manual and PC software](http://en.atorch.cn/NewsDetail.aspx?ID=96)

| File | Contents | Uploaded | Bytes |
|------|----------|----------|------:|
| `DK-02-BW600_PC_Application_V1.0.8.zip` | Original name `02-BW600_PC_Application_V1.0.8.zip`. The same PC software (identical `Load_Application.exe`), plus the BW600-DK user manual and the PC-software manual. | 2025-09-12 | 32,620,114 |
| `DK-BW600-6321-APP-2-0-5-UP.zip` | Original name `BW600-6321-APP-2-0-5-UP.zip`. Firmware V2.0.5. The `.bin` inside is **byte-identical** to the standard BW600 build above. | 2026-01-09 | 690,802 |
| `BW600-DK User manual.pdf` | The BW600-DK WiFi user manual, extracted unmodified from the DK PC-software zip, for convenience. | – | 3,978,550 |

The DK files have a `DK-` prefix added to their names; the table gives the original names.

The firmware `.bin` files are JieLi AC632N images. The main README describes how they're structured and encrypted.
They're flashed with the updater in the PC software. This project doesn't flash firmware.

## Sources

- `02-BW150_PC_Application_V1.0.8.zip`: <http://en.atorch.cn/upload/file/20250904/6389257655620603819049958.zip>
- `User Manual for Electronic Load Software.pdf`: <http://en.atorch.cn/upload/file/20250830/6389217582483042597381115.pdf>
- `BW600-6321-APP-2-0-3.zip`: <http://en.atorch.cn/upload/file/20250730/6388946922037347172301495.zip>
- `BW600-APP-2-0-5(定制取消长按+和-).zip`: <http://en.atorch.cn/upload/file/20251215/6390138629688870261082043.zip>
- `BW600-6321-APP-2-0-5-UP-2026.07.08.zip`: <http://en.atorch.cn/upload/file/20260708/6391910124187846482488973.zip>
- `DK-02-BW600_PC_Application_V1.0.8.zip`: <http://en.atorch.cn/upload/file/20250912/6389328277877168158921867.zip>
- `DK-BW600-6321-APP-2-0-5-UP.zip`: <http://en.atorch.cn/upload/file/20260109/6390357608264799245817541.zip>
- `BW600-DK User manual.pdf`: (extracted from DK-02-BW600_PC_Application_V1.0.8.zip)

## SHA-256

```
bd2d1b5012d5d51ec94a5caf3be9e74b52b3987fd3fd8cce9bf376f1951e4fd3  02-BW150_PC_Application_V1.0.8.zip
2bb657bf4d286eab7b068c4ee7aec41f080e8d7e28a08f0d205e9e76df0342e3  User Manual for Electronic Load Software.pdf
5722858dafd55b4b4c2e0dd9353d9c5480a99add49d2477d266b5d90509e1201  BW600-6321-APP-2-0-3.zip
90832b0cd488d9e087ab288790c48857404c6adef286f0d7a6b5223a1b1fe33f  BW600-APP-2-0-5(定制取消长按+和-).zip
3a524ea66e924065126c0b5ce44494ad95fd0cc40bf6feeb1532fea095fbc294  BW600-6321-APP-2-0-5-UP-2026.07.08.zip
c2f521ca264d3b10f7bda3cc325f3da9998e16ad1b71a5cfc98ca29368d6d9d6  DK-02-BW600_PC_Application_V1.0.8.zip
483597b63b090c2e2da8edd529e13cc691df6acd64983bb51d9e9406fd189b40  DK-BW600-6321-APP-2-0-5-UP.zip
1046c0a924333fb601e12a4c8ee84abc3b3efa92cde32a441559a79191c0c046  BW600-DK User manual.pdf
```

Check with `cd vendor && sha256sum -c SHA256SUMS`.
