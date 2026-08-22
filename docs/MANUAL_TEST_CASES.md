# 🧪 SKI Test Cases สำหรับทดสอบด้วยตนเอง (Manual Acceptance)

**URL ทดสอบ**
- **e2e stack**: http://localhost:18081 — login `e2e-admin` / `E2EAdminPass123!` (ข้อมูลทดสอบครบทุก KB)
- **Production**: https://knowledge.softnix.ai — login `admin` (ตาม `.env`)

> ทุกเคสค้นหาผ่านหน้า **ค้นหา (Search)** ของ KB นั้น ๆ — คำตอบต้องมาพร้อม Sources/การอ้างอิงเสมอ
> ถ้าคำตอบตอบว่า "ไม่พบหลักฐาน" ทั้งที่เคสนี้ควรเจอ → จดเป็น bug

---

## KB 1: กฎหมายไทย (E2E) — `e2e-thai-law`
| # | คำถามทดสอบ | สิ่งที่ควรเห็น |
|---|---|---|
| 1.1 | ประมวลกฎหมายแพ่งและพาณิชย์ มีลักษณะอย่างไร | สรุปเนื้อหาจาก "ประมวลกฎหมายแพ่งและพาณิชย์ — บทสรุป" + Sources |
| 1.2 | กฎหมายคืออะไร ทำหน้าที่อะไรบ้าง | คำอธิบาย "กฎหมายเป็นระบบของกฎและแบบแผน..." + Sources |
| 1.3 | ประมวลกฎหมายแพ่งและพาณิชย์ มาตรา 6 ว่าอย่างไร | ต้องไม่ชน 404/เออเรอ — อาจตอบว่าไม่พบมาตรา 6 ในเอกสาร (บทสรุปไม่มีข้อความมาตรา) พร้อม sources ที่เกี่ยวข้อง |
| 1.4 | *(OCR)* เปิดเอกสาร "กฎหมายไทย — สำเนาสแกน (OCR test)" → แท็บ เนื้อหา | ข้อความไทยอ่านได้จาก OCR ไม่ใช่ภาษาตุรกี/ขยะ |
| 1.5 | *(Explore graph)* สำรวจกราฟ → แผนที่กฎหมาย | ตราสาร 2 ฉบับ (บทสรุป + OCR test), สถานะ "ไม่ทราบสถานะ" แสดงถูก |

## KB 2: กฎหมาย/มาตรฐานสากล (E2E) — `e2e-intl-standards`
| # | คำถามทดสอบ | สิ่งที่ควรเห็น |
|---|---|---|
| 2.1 | What does HTTP 409 mean | อธิบาย 409 Conflict จาก RFC 2616 + Sources |
| 2.2 | self-attention mechanism คืออะไร (TH) | ตอบจาก... ⚠️ ระวัง: arXiv อยู่ KB เทคนิค — KB นี้ต้องไม่ดึงข้าม KB ถ้าตอบได้แปลว่า leak |
| 2.3 | NIST cybersecurity framework มีฟังก์ชันอะไรบ้าง | 5 functions (Identify/Protect/Detect/Respond/Recover) จาก NIST doc |
| 2.4 | HTTP/9.9 คืออะไร | RFC 9999 (เอกสารทดสอบ title มี `/`) — ต้องค้นเจอ ไม่พังจาก slash |
| 2.5 | *(MCP)* tools/call `find_entities` search_text="HTTP" | เจอ entity "RFC 2616 HTTP v1.1" |

## KB 3: นโยบายและ IT Policy (E2E) — `e2e-it-policy`
| # | คำถามทดสอบ | สิ่งที่ควรเห็น |
|---|---|---|
| 3.1 | วิธีขอสิทธิ์ VPN ทำอย่างไร | ขั้นตอนจาก IT policy doc (intent how_to) + Sources |
| 3.2 | RFC 2616 ใน KB นี้มีเนื้อหาต่างจากต้นฉบับไหม | ตอบจาก "technical notes" เวอร์ชัน policy — ทดสอบว่าเนื้อหาคล้ายกันข้าม KB ไม่ชน dedup แล้ว |
| 3.3 | *(cross-KB)* ถามคำถาม NIST ใน KB นี้ | ไม่พบหลักฐาน/insufficient ต้องแสดงสวยงาม ไม่ดึง NIST จาก intl-standards ข้าม KB |

## KB 4: ข้อมูลภาครัฐ (E2E) — `e2e-gov-data`
| # | คำถามทดสอบ | สิ่งที่ควรเห็น |
|---|---| Regression? |
|---|---|---|
| 4.1 | ปริมาณน้ำฝนกรุงเทพ (ประโยคไทยเต็ม) | **เคสสำคัญ — เคยได้ 0 sources ก่อน fix Thai planner** ต้องได้ sources จาก climate CSV/XLSX/JSON |
| 4.2 | precipitation in Bangkok | sources จาก climate docs |
| 4.3 | *(Formats)* เปิด 3 เอกสาร climate (CSV/JSON/XLSX) → แท็บ เนื้อหา | ทั้งสาม format แสดงข้อมูลอ่านได้ (XLSX เคยพัง F4) |
| 4.4 | *(Dedup)* ลองอัปโหลดไฟล์เดิมซ้ำ (bangkok-climate CSV) | ต้องได้ 409 FILE_DUPLICATE ไม่ใช่ 500 |

## KB 5: เทคนิค/วิชาการ (E2E) — `e2e-technical`
| # | คำถามทดสอบ | สิ่งที่ควรเห็น |
|---|---|---|
| 5.1 | self-attention mechanism คืออะไร | อธิบายจาก arXiv "Attention Is All You Need" + Sources |
| 5.2 | Transformer architecture มีส่วนประกอบอะไร | encoder-decoder + multi-head attention จาก paper |
| 5.3 | *(MCP)* find_entities search_text="attention" | เจอ entity ที่เกี่ยวกับ attention/paper |

## KB 6: เอกสารทั่วไป/คู่มือ (E2E) — `e2e-general-docs`
| # | คำถามทดสอบ | สิ่งที่ควรเห็น |
|---|---|---|
| 6.1 | สไลด์ภาพรวมกฎหมายไทยพูดถึงอะไรบ้าง | สรุปจาก PPTX "ภาพรวมกฎหมายไทย — สไลด์บรรยาย" |
| 6.2 | ประมวลกฎหมายแพ่งและพาณิชย์ คืออะไร (คำถามคล้าย KB1) | ตอบจากเอกสารวิกิพีเดียใน KB นี้เอง ไม่ลากจาก KB1 ข้าม KB |
| 6.3 | *(Delete cascade)* ลบเอกสาร "dedup-verify-general" แล้วถามเนื้อหาเดิม | ไม่พบในผลค้นหาอีก (ghost ถูก purge จาก LightRAG) — เอกสารนี้เป็นของทดสอบ ลบได้ |

## ข้าม KB / ระบบ
| # | คำถามทดสอบ | สิ่งที่ควรเห็น |
|---|---|---|
| S1 | *(Rate limit)* กดค้นหาถี่ ๆ 60+ ครั้งใน 1 นาที (token เดียวกัน) | แจ้ง rate limit ที่ครั้งที่ 61 ไม่ใช่ตายเงียบ |
| S2 | *(F6 fallback)* ถามคำถามที่ LLM ตอบไม่ cite เช่น "สรุป NIST ทั้งฉบับ" | คำตอบอาจเป็น "พบหลักฐานที่เกี่ยวข้อง [S#]..." + รายการแหล่งอ้างอิง — ไม่ใช่ 0 sources |
| S3 | *(2 ภาษา)* สลับ EN/TH มุมมองเอกสาร/ค้นหา | ข้อความไทยราบรื่น ไม่มีประโยคอังกฤษโผล่กลางไทย (หลัง fix UxUI) |
| S4 | *(Ingest)* หน้า Ingest API → สร้าง token → อัปโหลดไฟล์ด้วย curl snippet ที่ UI ให้ | 202 + เอกสารเข้าคิว ประมวลผลจน completed |
| S5 | *(MCP)* เชื่อม Claude/external client ผ่าน MCP endpoint + token ใหม่ 9 tools | tools/list ได้ 9 เครื่องมือ ครบ search/entities/graph/legal |

---

## สิ่งที่ห้ามทำระหว่างทดสอบ
- ❌ อย่าลบ KB หรือเอกสารอื่นนอกจากที่ระบุ (dedup-verify-general ตัวเดียวที่ลบได้)
- ❌ อย่า reset/recreate stack — corpus ทั้งหมดหาย
- ✅ อัปโหลดเอกสารใหม่เพิ่มได้อิสระ
