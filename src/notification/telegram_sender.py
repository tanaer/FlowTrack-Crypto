"""Telegram消息发送模块

负责将分析结果发送到Telegram频道。
"""
import logging
import os
import io
import textwrap
import qrcode
from typing import Optional
from telegram import Bot
from telegram.constants import ParseMode
from PIL import Image, ImageDraw, ImageFont
from bs4 import BeautifulSoup
import markdown
from ..config import config

# 配置日志
logger = logging.getLogger(__name__)

async def send_telegram_message_async(message: str, as_image: bool = True) -> bool:
    """异步发送Telegram消息
    
    Args:
        message: 要发送的消息文本
        as_image: 是否将消息转为图片发送
        
    Returns:
        发送成功返回True，失败返回False
    """
    try:
        bot_token = config.get('TELEGRAM', 'BOT_TOKEN')
        chat_id = config.get('TELEGRAM', 'CHAT_ID')
        
        bot = Bot(token=bot_token)
        
        if as_image:
            # 将消息转换为图片
            image_buffer = text_to_image(message)
            if image_buffer:
                # 异步发送图片
                await bot.send_photo(chat_id=chat_id, photo=image_buffer)
                logger.info("成功发送Telegram图片消息")
                return True
            else:
                logger.error("图片生成失败，消息未发送")
                return False
        else:
            # 发送文本消息
            await bot.send_message(chat_id=chat_id, text=message, parse_mode=ParseMode.MARKDOWN)
            logger.info("成功发送Telegram文本消息")
            return True
    except Exception as e:
        logger.error(f"发送Telegram消息失败: {e}")
        return False


def text_to_image(text: str, watermark: str = "Telegram: @jin10light") -> Optional[io.BytesIO]:
    """将Markdown文本转换为图片，并添加水印
    
    Args:
        text: Markdown格式的文本
        watermark: 要添加的水印文本
        
    Returns:
        图片缓冲区，转换失败则返回None
    """
    try:
        # 设置字体和颜色
        background_color = (255, 255, 255)  # 白色背景
        text_color = (30, 30, 30)          # 深灰色文字
        title_color = (0, 0, 0)            # 黑色标题
        table_header_bg = (240, 240, 240)  # 表头背景
        table_border_color = (200, 200, 200)  # 表格边框
        watermark_color = (180, 160, 160)  # 浅灰色水印

        # 准备字体
        try:
            # 尝试使用常见中文字体
            font_paths = [
                "AlibabaPuHuiTi-3-55-Regular.ttf", 
                "C:/Windows/Fonts/msyh.ttc", 
                "C:/Windows/Fonts/simhei.ttf",
                "/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc"
            ]
            
            font_path = None
            for path in font_paths:
                if os.path.exists(path):
                    font_path = path
                    break
                    
            if font_path:
                regular_font = ImageFont.truetype(font_path, 16)
                title_font = ImageFont.truetype(font_path, 24)
                subtitle_font = ImageFont.truetype(font_path, 20)
                small_font = ImageFont.truetype(font_path, 16)
            else:
                # 使用默认字体
                regular_font = ImageFont.load_default()
                title_font = ImageFont.load_default()
                subtitle_font = ImageFont.load_default()
                small_font = ImageFont.load_default()
        except Exception as e:
            logger.warning(f"加载字体失败: {e}, 使用默认字体")
            regular_font = ImageFont.load_default()
            title_font = ImageFont.load_default()
            subtitle_font = ImageFont.load_default() 
            small_font = ImageFont.load_default()
        
        # 预处理：查找和替换mermaid代码块
        mermaid_pattern = r"```mermaid\s+(.*?)\s+```"
        import re
        text = re.sub(mermaid_pattern, r"**图表内容:** \1", text, flags=re.DOTALL)
            
        # 将Markdown转换为HTML
        html = markdown.markdown(text, extensions=['tables', 'fenced_code'])
        soup = BeautifulSoup(html, 'html.parser')
        
        # 计算图像大小
        padding = 30  # 内边距
        line_height = 28  # 行高
        
        # 创建临时图像用于测量文本大小
        temp_img = Image.new('RGB', (1, 1), background_color)
        draw = ImageDraw.Draw(temp_img)
        
        # 找出最长的中文文本行并计算宽度
        max_text_width = 0
        raw_lines = text.split('\n')
        
        # 计算每一行的宽度
        for line in raw_lines:
            # 跳过空行
            if not line.strip():
                continue
                
            # 计算行宽度
            line_width = draw.textlength(line, font=regular_font)
            max_text_width = max(max_text_width, line_width)
        
        # 为中文表格头部进行特殊处理（通常这些是最宽的）
        table_headers = []
        for line in raw_lines:
            if line.startswith('|') and '|' in line[1:] and not line.strip().startswith('|-'):
                header_items = [item.strip() for item in line.split('|') if item.strip()]
                for item in header_items:
                    table_headers.append(item)
        
        # 计算表头项的宽度
        for header in table_headers:
            header_width = draw.textlength(header, font=regular_font)
            # 中文表头需要额外空间以防重叠
            if any('\u4e00' <= ch <= '\u9fff' for ch in header):  # 检测是否包含中文字符
                header_width *= 1.3  # 为中文字符增加30%的宽度
            max_text_width = max(max_text_width, header_width)
            
        # 标题通常更宽
        titles = []
        for line in raw_lines:
            if line.startswith('#'):
                title_text = line.lstrip('#').strip()
                titles.append(title_text)
                
        for title in titles:
            font = title_font if line.startswith('# ') else subtitle_font
            title_width = draw.textlength(title, font=font)
            max_text_width = max(max_text_width, title_width)
        
        # 计算基础宽度 - 添加两倍内边距并确保最小宽度
        base_width = max_text_width + 2 * padding
        min_img_width = 1200  # 最小宽度
        
        # 估算宽度和高度 - 确保中文表格有足够空间
        if any('\u4e00' <= ch <= '\u9fff' for ch in text):  # 检测是否包含中文字符
            # 如果是中文内容，给予更多空间
            min_img_width = max(min_img_width, int(base_width * 1.2))  # 增加20%的宽度
        
        # 预处理表格内容
        tables = soup.find_all('table')
        table_column_widths = []
        
        for table in tables:
            # 计算表格每列的最大宽度
            rows = table.find_all('tr')
            if not rows:
                continue
                
            # 找出最大列数
            max_cols = max(len(row.find_all(['th', 'td'])) for row in rows)
            column_widths = [0] * max_cols
            
            # 计算每列内容的最大宽度
            for row in rows:
                cells = row.find_all(['th', 'td'])
                for i, cell in enumerate(cells):
                    if i < max_cols:
                        text_width = draw.textlength(cell.text.strip(), font=regular_font)
                        # 中文单元格需要更多空间
                        if any('\u4e00' <= ch <= '\u9fff' for ch in cell.text.strip()):
                            text_width *= 1.3
                        column_widths[i] = max(column_widths[i], text_width)
            
            # 确保每列至少有合适的宽度
            column_widths = [max(w + 40, 120) for w in column_widths]  # 增加内边距
            table_column_widths.append(column_widths)
            
            # 检查表格总宽度是否足够
            table_width = sum(column_widths) + 2 * padding
            min_img_width = max(min_img_width, table_width)
        
        # 估算总高度
        total_height = 0
        
        for element in soup.find_all(['h1', 'h2', 'h3', 'p', 'ul', 'ol', 'table', 'pre']):
            if element.name.startswith('h'):
                level = int(element.name[1])
                font = title_font if level == 1 else subtitle_font
                text_width = draw.textlength(element.text, font=font)
                total_height += line_height * (1.8 if level == 1 else 1.5)  # 增加标题行高
            elif element.name == 'p':
                # 段落文本分行
                text = element.text
                wrapped_text = textwrap.wrap(text, width=int(min_img_width / 10))  # 根据图片宽度确定每行字符数
                total_height += len(wrapped_text) * line_height
            elif element.name == 'table':
                # 表格高度估算
                rows = element.find_all('tr')
                total_rows = len(rows)
                # 增加表格行高和表格间距
                total_height += total_rows * line_height * 1.8 + 40
            elif element.name in ('ul', 'ol'):
                # 列表项
                items = element.find_all('li')
                total_height += len(items) * line_height * 1.2 + 20
            elif element.name == 'pre':
                # 代码块
                code_lines = element.text.strip().split('\n')
                total_height += len(code_lines) * line_height + 30
        
        # 添加页脚空间
        total_height += 150
        
        # 创建图像 - 使用计算出的宽度
        img_width = min_img_width
        img_height = int(total_height + 2 * padding)
        
        logger.info(f"图片尺寸: 宽度={img_width}, 高度={img_height}, 最长文本宽度={max_text_width}")
        
        # 确保图像尺寸为整数
        img_width = int(img_width)
        img_height = int(img_height)
        
        image = Image.new('RGB', (img_width, img_height), background_color)
        draw = ImageDraw.Draw(image)
        
        # 绘制内容
        y_position = padding
        table_index = 0
        
        for element in soup.find_all(['h1', 'h2', 'h3', 'p', 'ul', 'ol', 'table', 'pre']):
            if element.name.startswith('h'):
                level = int(element.name[1])
                font = title_font if level == 1 else subtitle_font
                # 增加标题间距
                if y_position > padding:
                    y_position += 15
                
                draw.text((padding, y_position), element.text, font=font, fill=title_color)
                y_position += line_height * (1.8 if level == 1 else 1.5)
                
                # 为h1添加下划线
                if level == 1:
                    draw.line([(padding, y_position - 5), (img_width - padding, y_position - 5)], 
                             fill=table_border_color, width=2)
                    y_position += 10  # 标题下方增加间距
            
            elif element.name == 'p':
                text = element.text
                wrapped_text = textwrap.wrap(text, width=int(img_width / 10))  # 根据图片宽度确定每行字符数
                for line in wrapped_text:
                    draw.text((padding, y_position), line, font=regular_font, fill=text_color)
                    y_position += line_height
                y_position += 10  # 段落间距增加
            
            elif element.name == 'table':
                # 绘制表格
                rows = element.find_all('tr')
                if not rows:
                    continue
                
                column_widths = table_column_widths[table_index] if table_index < len(table_column_widths) else []
                table_index += 1
                
                if not column_widths:
                    # 如果没有计算出列宽，使用均等宽度
                    max_cols = max(len(row.find_all(['th', 'td'])) for row in rows)
                    total_width = img_width - 2 * padding
                    column_widths = [total_width / max_cols] * max_cols
                
                # 表格开始位置
                table_top = y_position
                table_left = padding
                # 增加表格前的间距
                y_position += 15
                
                # 计算表格总宽度
                table_width = sum(column_widths)
                
                for row_idx, row in enumerate(rows):
                    cells = row.find_all(['th', 'td'])
                    row_height = line_height * 1.8  # 增加行高
                    
                    # 绘制行背景
                    row_bg_color = table_header_bg if row_idx == 0 else background_color
                    draw.rectangle([(table_left, y_position), 
                                    (table_left + table_width, y_position + row_height)], 
                                  fill=row_bg_color)
                    
                    # 绘制单元格内容
                    x_pos = table_left
                    for col_idx, cell in enumerate(cells):
                        if col_idx < len(column_widths):
                            cell_width = column_widths[col_idx]
                            cell_text = cell.text.strip()
                            
                            # 调整单元格文本颜色 (可以根据内容设置不同颜色)
                            cell_color = text_color
                            if "看涨" in cell_text or "做多" in cell_text:
                                cell_color = (0, 130, 0)  # 绿色
                            elif "看跌" in cell_text or "做空" in cell_text:
                                cell_color = (200, 0, 0)  # 红色
                            
                            # 文本换行处理
                            wrapped_cell_text = textwrap.wrap(cell_text, width=int(cell_width/10))  # 根据单元格宽度换行
                            text_y = y_position + 5  # 单元格内边距
                            
                            if wrapped_cell_text:
                                for line in wrapped_cell_text:
                                    # 计算文本位置（居中）
                                    text_width = draw.textlength(line, font=regular_font)
                                    text_x = x_pos + (cell_width - text_width) / 2
                                    draw.text((text_x, text_y), line, font=regular_font, fill=cell_color)
                                    text_y += line_height
                            
                            # 绘制单元格边框
                            draw.line([(x_pos, y_position), (x_pos, y_position + row_height)], 
                                     fill=table_border_color, width=1)
                            
                            x_pos += cell_width
                    
                    # 绘制右侧边框和底部边框
                    draw.line([(table_left + table_width, y_position), 
                               (table_left + table_width, y_position + row_height)], 
                             fill=table_border_color, width=1)
                    draw.line([(table_left, y_position + row_height), 
                               (table_left + table_width, y_position + row_height)], 
                             fill=table_border_color, width=1)
                    
                    y_position += row_height
                
                y_position += 20  # 表格底部额外间距
            
            elif element.name in ('ul', 'ol'):
                for idx, item in enumerate(element.find_all('li')):
                    bullet = '• ' if element.name == 'ul' else f"{idx+1}. "
                    item_text = bullet + item.text
                    wrapped_lines = textwrap.wrap(item_text, width=int(img_width / 14))  # 根据图片宽度确定每行字符数
                    
                    for line_idx, line in enumerate(wrapped_lines):
                        # 第一行使用项目符号，后续行缩进对齐
                        if line_idx == 0:
                            draw.text((padding, y_position), line, font=regular_font, fill=text_color)
                        else:
                            # 缩进与项目符号对齐
                            indent = draw.textlength(bullet, font=regular_font)
                            draw.text((padding + indent, y_position), line, font=regular_font, fill=text_color)
                        y_position += line_height
                
                y_position += 10  # 列表底部额外间距
            
            elif element.name == 'pre':
                # 绘制代码块
                code_text = element.text.strip()
                code_lines = code_text.split('\n')
                
                # 代码块背景
                code_bg_height = len(code_lines) * line_height + 20
                draw.rectangle([(padding - 10, y_position - 10), 
                                (img_width - padding + 10, y_position + code_bg_height)], 
                              fill=(245, 245, 245))  # 浅灰色背景
                
                for code_line in code_lines:
                    draw.text((padding + 5, y_position + 5), code_line, font=small_font, fill=text_color)
                    y_position += line_height
                
                y_position += 20  # 代码块底部额外间距
        
        # 添加水印和二维码
        # 对角线水印
        watermark_font = small_font
        watermark_text_width = draw.textlength(watermark, font=watermark_font)
        
        for i in range(0, img_width + img_height, 200):  # 减少水印密度
            x = max(0, i - img_height)
            y = max(0, img_height - i)
            draw.text((x + 150, y + 50), watermark, font=watermark_font, fill=watermark_color)
        
        # 添加免责声明底部水印
        disclaimer = "免责声明：本分析仅供专业参考，不构成投资建议，交易决策请自行承担风险"
        draw.text((padding, img_height - 40), disclaimer, font=small_font, fill=text_color)
        
        # 创建二维码
        qr = qrcode.QRCode(
            version=1,
            error_correction=qrcode.constants.ERROR_CORRECT_L,
            box_size=10,
            border=2,
        )
        qr.add_data("https://t.me/jin10light")
        qr.make(fit=True)
        qr_img = qr.make_image(fill_color="black", back_color="white")
        
        # 调整二维码大小
        qr_size = 160
        qr_img = qr_img.resize((qr_size, qr_size))
        
        # 将二维码放在右下角
        image.paste(qr_img, (img_width - qr_size - padding, img_height - qr_size - padding))
        
        # 保存到内存
        buffer = io.BytesIO()
        image.save(buffer, format="PNG")
        buffer.seek(0)
        
        return buffer
    except Exception as e:
        logger.error(f"生成图片失败: {e}")
        import traceback
        logger.error(traceback.format_exc())
        return None 