import os
import logging
from logging.handlers import TimedRotatingFileHandler

def setup_logger(operation_name):
    """为特定操作创建并配置日志记录器"""
    if not os.path.exists(os.path.join(os.getcwd(), 'log')):
        # 如果文件夹不存在，则创建
        os.makedirs(os.path.join(os.getcwd(), 'log'))
    # 创建日志记录器

    logger = logging.getLogger(operation_name)
    #  如果有了日志处理器则直接返回
    if logger.hasHandlers():
        return logger
    logger.setLevel(logging.INFO)

    # 确保每个操作都有独立的日志处理器
    if not logger.hasHandlers():
        # 创建文件处理器（使用 TimedRotatingFileHandler）
        log_filename = os.path.join(os.getcwd(), 'log', f"{operation_name}.log")
        file_handler = TimedRotatingFileHandler(log_filename, when='midnight', interval=1, backupCount=7)
        file_handler.setLevel(logging.INFO)

        # 创建格式化器并设置
        formatter = logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s')
        file_handler.setFormatter(formatter)

        # 将处理器添加到日志记录器
        logger.addHandler(file_handler)
        # 防止日志传播到根记录器
    logger.propagate = False
    return logger
