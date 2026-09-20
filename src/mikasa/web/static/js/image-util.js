/* =========================================================================
   图片工具（M6 ② 拍照/图片转笔记）：读图 → 等比缩放 → 重编码。

   一个实现、两处用：设置面板的聊天背景图（要 dataURL、透明区垫底色）与
   笔记编辑器的识图上传（要 Blob，FormData 直接 append）。这段逻辑先前长在
   settings.js 里；原样复制一份到笔记编辑器，迟早会在某次改动后漂移——
   一边压到 1600、另一边把手机原图整张发出去（十几 MB，先不说慢，
   识图端点 12MB 的上限会直接回 413，用户看到的是"上传失败"）。
   ========================================================================= */

export const MAX_EDGE = 1600; // 压缩后的最长边（够识图看清字，又不至于太大）
const JPEG_QUALITY = 0.85;
const PAD_COLOR = "#faf9f5"; // 透明区垫画布色（与 --bg 同值，换色板时要一起改）

/** 读文件 → 解码成 <img>。 */
async function decode(file) {
  const raw = await new Promise((resolve, reject) => {
    const fr = new FileReader();
    fr.onload = () => resolve(fr.result);
    fr.onerror = () => reject(new Error("读取图片失败"));
    fr.readAsDataURL(file);
  });
  return await new Promise((resolve, reject) => {
    const node = new Image();
    node.onload = () => resolve(node);
    node.onerror = () => reject(new Error("图片解码失败（这个格式浏览器可能不支持）"));
    node.src = raw;
  });
}

/** 等比缩到最长边 maxEdge → 画到 canvas（透明区垫底色）。 */
async function toCanvas(file, { maxEdge = MAX_EDGE } = {}) {
  const img = await decode(file);
  const scale = Math.min(1, maxEdge / Math.max(img.width, img.height));
  const canvas = document.createElement("canvas");
  canvas.width = Math.max(1, Math.round(img.width * scale));
  canvas.height = Math.max(1, Math.round(img.height * scale));
  const ctx = canvas.getContext("2d");
  ctx.fillStyle = PAD_COLOR;
  ctx.fillRect(0, 0, canvas.width, canvas.height);
  ctx.drawImage(img, 0, 0, canvas.width, canvas.height);
  return canvas;
}

function mb(bytes) {
  return (bytes / 1048576).toFixed(1);
}

/**
 * 图片 → 压缩后的 Blob（上传用）。
 *
 * 超限直接抛错、**不发请求**：让用户在编辑器里就看到"换一张更小的"，
 * 而不是等一个 413 回来再猜哪里不对。
 */
export async function compressImage(file, { maxEdge = MAX_EDGE, maxBytes = 8 * 1048576 } = {}) {
  const canvas = await toCanvas(file, { maxEdge });
  const blob = await new Promise((resolve) => canvas.toBlob(resolve, "image/jpeg", JPEG_QUALITY));
  if (!blob) throw new Error("图片编码失败");
  if (blob.size > maxBytes) {
    throw new Error(`图片太大（压缩后 ${mb(blob.size)}MB，上限 ${mb(maxBytes)}MB），请换一张更小的`);
  }
  return blob;
}

/** 图片 → 压缩后的 dataURL（本机存储用：聊天背景图进 localStorage）。 */
export async function imageToDataUrl(
  file,
  { maxEdge = MAX_EDGE, maxBytes = 4 * 1048576 } = {}
) {
  const canvas = await toCanvas(file, { maxEdge });
  const dataUrl = canvas.toDataURL("image/jpeg", JPEG_QUALITY);
  if (dataUrl.length > maxBytes) throw new Error("图片太大，请换一张更小的（约 4MB 内）");
  return dataUrl;
}
