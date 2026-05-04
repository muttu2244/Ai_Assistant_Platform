"""Image watermarking utilities for embedding creator metadata.

Provides both visible watermark embedding and EXIF metadata tagging
to protect IP and provide proof of authorship.
"""

from io import BytesIO
import logging
from pathlib import Path
from typing import Optional

try:
	from PIL import Image
	from PIL.PngImagePlugin import PngInfo
	HAS_PIL = True
except ImportError:
	HAS_PIL = False

logger = logging.getLogger(__name__)


def embed_metadata_watermark(
	image_path_or_bytes: str | Path | bytes,
	creator: str = "Developed by Shivayogi",
	copyright_notice: str = "© 2026 SmartCare AI Platform. All rights reserved.",
	output_path: Optional[str | Path] = None,
) -> bytes:
	"""Embed metadata watermark into an image file (EXIF/PNG info).
	
	Args:
		image_path_or_bytes: Path to image file or raw image bytes
		creator: Creator/artist name to embed in metadata
		copyright_notice: Copyright text to embed in metadata
		output_path: Optional path to save watermarked image. If None, returns bytes.
	
	Returns:
		Watermarked image as bytes (if output_path is None)
		
	Raises:
		ImportError: If Pillow is not installed
		IOError: If image cannot be read/processed
	"""
	if not HAS_PIL:
		logger.warning(
			"Pillow not installed. Metadata watermarking skipped. "
			"Install with: pip install pillow piexif"
		)
		# Return original bytes if available
		if isinstance(image_path_or_bytes, bytes):
			return image_path_or_bytes
		return Path(image_path_or_bytes).read_bytes()
	
	# Load image
	if isinstance(image_path_or_bytes, bytes):
		img = Image.open(BytesIO(image_path_or_bytes))
	else:
		img = Image.open(image_path_or_bytes)
	
	# Determine format
	fmt = img.format or "PNG"
	
	# Embed metadata based on format
	if fmt.upper() == "PNG":
		# PNG uses PngInfo
		metadata = PngInfo()
		metadata.add_text("Creator", creator)
		metadata.add_text("Copyright", copyright_notice)
		metadata.add_text("Software", "SmartCare AI Platform")
		
		output = BytesIO()
		img.save(output, format="PNG", pnginfo=metadata)
		result = output.getvalue()
		
	elif fmt.upper() in ("JPEG", "JPG"):
		# JPEG metadata via exif (requires piexif)
		try:
			import piexif
		except ImportError:
			logger.warning(
				"piexif not installed. JPEG metadata embedding skipped. "
				"Install with: pip install piexif"
			)
			output = BytesIO()
			img.save(output, format="JPEG", quality=95)
			result = output.getvalue()
		else:
			# Extract existing EXIF or create new
			try:
				exif_dict = piexif.load(image_path_or_bytes if isinstance(image_path_or_bytes, (str, Path)) else BytesIO(image_path_or_bytes))
			except Exception:
				exif_dict = {"0th": {}, "Exif": {}, "GPS": {}, "1st": {}}
			
			# Add/update copyright and artist tags
			exif_dict["0th"][piexif.ImageIFD.Artist] = creator.encode("utf8")
			exif_dict["0th"][piexif.ImageIFD.Copyright] = copyright_notice.encode("utf8")
			
			# Serialize and embed
			exif_bytes = piexif.dump(exif_dict)
			output = BytesIO()
			img.save(output, format="JPEG", quality=95, exif=exif_bytes)
			result = output.getvalue()
	else:
		# Fallback: just save as-is (metadata not guaranteed)
		logger.info(f"Format {fmt} has limited metadata support. Saving without EXIF.")
		output = BytesIO()
		img.save(output, format=fmt or "PNG")
		result = output.getvalue()
	
	# Save to file if requested
	if output_path:
		Path(output_path).write_bytes(result)
		logger.info(f"Watermarked image saved to {output_path}")
	
	return result


def add_visible_watermark(
	image_path_or_bytes: str | Path | bytes,
	watermark_text: str = "Developed by Shivayogi",
	opacity: float = 0.3,
	output_path: Optional[str | Path] = None,
) -> bytes:
	"""Add a visible text watermark to an image.
	
	Args:
		image_path_or_bytes: Path to image file or raw image bytes
		watermark_text: Text to display as watermark
		opacity: Opacity of watermark text (0.0 - 1.0)
		output_path: Optional path to save watermarked image
	
	Returns:
		Watermarked image as bytes
		
	Raises:
		ImportError: If Pillow is not installed
	"""
	if not HAS_PIL:
		logger.warning("Pillow not installed. Visible watermarking skipped.")
		if isinstance(image_path_or_bytes, bytes):
			return image_path_or_bytes
		return Path(image_path_or_bytes).read_bytes()
	
	# Load image
	if isinstance(image_path_or_bytes, bytes):
		img = Image.open(BytesIO(image_path_or_bytes))
		img = img.convert("RGBA")
	else:
		img = Image.open(image_path_or_bytes)
		img = img.convert("RGBA")
	
	# Create transparent watermark layer
	watermark = Image.new("RGBA", img.size, (0, 0, 0, 0))
	
	try:
		from PIL import ImageDraw, ImageFont
		draw = ImageDraw.Draw(watermark)
		
		# Try to use a nice font, fall back to default
		try:
			font = ImageFont.truetype("arial.ttf", size=20)
		except Exception:
			font = ImageFont.load_default()
		
		# Position watermark in bottom-right
		text_color = (0, 219, 198, int(255 * opacity))  # Accent color with opacity
		bbox = draw.textbbox((0, 0), watermark_text, font=font)
		text_width = bbox[2] - bbox[0]
		text_height = bbox[3] - bbox[1]
		
		x = img.width - text_width - 15
		y = img.height - text_height - 10
		draw.text((x, y), watermark_text, fill=text_color, font=font)
		
	except Exception as e:
		logger.warning(f"Failed to apply visible watermark: {e}")
	
	# Composite watermark onto image
	result_img = Image.alpha_composite(img, watermark)
	result_img = result_img.convert("RGB")
	
	# Save
	output = BytesIO()
	result_img.save(output, format="PNG")
	result = output.getvalue()
	
	if output_path:
		Path(output_path).write_bytes(result)
		logger.info(f"Watermarked image saved to {output_path}")
	
	return result


def watermark_image_complete(
	image_path_or_bytes: str | Path | bytes,
	creator: str = "Developed by Shivayogi",
	copyright_notice: str = "© 2026 SmartCare AI Platform. All rights reserved.",
	add_visible: bool = False,
	output_path: Optional[str | Path] = None,
) -> bytes:
	"""Apply both metadata and optional visible watermarking.
	
	This is the recommended approach for full IP protection.
	
	Args:
		image_path_or_bytes: Path to image or raw bytes
		creator: Creator name for metadata
		copyright_notice: Copyright text for metadata
		add_visible: If True, also add a visible watermark
		output_path: Optional path to save result
	
	Returns:
		Fully watermarked image as bytes
	"""
	# Start with metadata watermark
	result = embed_metadata_watermark(
		image_path_or_bytes,
		creator=creator,
		copyright_notice=copyright_notice,
	)
	
	# Add visible watermark if requested
	if add_visible:
		result = add_visible_watermark(
			result,
			watermark_text=creator,
			opacity=0.3,
		)
	
	# Save to file if requested
	if output_path:
		Path(output_path).write_bytes(result)
		logger.info(f"Complete watermarked image saved to {output_path}")
	
	return result
