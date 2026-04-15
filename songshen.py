import time
import sys
from selenium import webdriver
from selenium.webdriver.edge.service import Service
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.common.exceptions import TimeoutException, NoSuchElementException, ElementClickInterceptedException

# --- 配置区域 ---
EDGE_DRIVER_PATH = 'msedgedriver.exe'  # 如果已配置环境变量，可直接写 'msedgedriver'
TARGET_URL = "https://member.casmart.com.cn/supplier/offline/bill/list?client-key=eadd9901df4bdaed0b2b1d95e0c77534"  # 替换为你的实际网址
WAIT_TIME = 10  # 等待元素加载的超时时间
# ------------------

def init_driver():
    """初始化 Edge 浏览器"""
    options = webdriver.EdgeOptions()
    # 如果需要保持登录状态，可以添加用户数据目录（可选，根据实际情况配置）
    # options.add_argument(r"--user-data-dir=C:\Users\YourUser\AppData\Local\Microsoft\Edge\User Data")

    service = Service(EDGE_DRIVER_PATH)
    driver = webdriver.Edge(service=service, options=options)
    driver.maximize_window()
    return driver

def process_current_page(driver):
    """
    处理当前页面的所有送审任务
    返回: True 如果处理了至少一个单据，False 如果当前页没有发现送审按钮
    """
    processed_any = False

    while True:
        try:
            # 1. 寻找页面上的“送审”按钮
            # 根据截图，按钮文本是“送审”。XPath 使用 text() 匹配。
            # 为了防止点击到隐藏元素，我们显式等待它可点击
            send_btn = WebDriverWait(driver, 5).until(
                EC.element_to_be_clickable((By.XPATH, "//button/span[text()='送审']/ancestor::button"))
            )

            # 滚动到元素可见位置（防止被页脚遮挡）
            driver.execute_script("arguments[0].scrollIntoView(true);", send_btn)
            time.sleep(0.5)

            print(f"找到送审按钮，正在点击... (当前时间: {time.strftime('%H:%M:%S')})")
            send_btn.click()

            # 2. 处理弹窗：等待并点击“确定”按钮
            # 截图显示弹窗标题是“审批流类型选择”，确认按钮是“确定”
            confirm_btn = WebDriverWait(driver, 10).until(
                EC.element_to_be_clickable((By.XPATH, "//button/span[text()='确定']/ancestor::button"))
            )

            print("发现弹窗，正在点击确定...")
            confirm_btn.click()

            # 3. 等待操作反馈（比如弹窗消失或提示成功）
            # 这里简单等待1秒，防止点击过快导致页面未响应
            time.sleep(1.5)
            processed_any = True

        except TimeoutException:
            # 如果在短时间内找不到“送审”按钮，说明当前页面处理完毕
            print("当前页面未发现更多送审按钮。")
            break
        except Exception as e:
            print(f"发生未知错误: {e}")
            break

    return processed_any

def click_next_page(driver):
    """
    尝试点击分页栏的“下一页”
    返回: True 如果成功翻页，False 如果已是最后一页或无法翻页
    """
    try:
        # 寻找“下一页”按钮。通常分页按钮包含类名 'next' 或者文本 '下一页'
        # 这里假设使用了常见的 Element UI 或类似的分页组件
        next_btn = WebDriverWait(driver, 5).until(
            EC.element_to_be_clickable((By.XPATH, "//button[@class='btn-next' and not(contains(@class, 'is-disabled'))] | //li[text()='下一页' and not(contains(@class, 'disabled'))]"))
        )

        # 再次检查是否真的可点击（有些框架虽然不报错但 class 里带有 disabled）
        if "disabled" in next_btn.get_attribute("class"):
            return False

        print("正在翻到下一页...")
        driver.execute_script("arguments[0].click();", next_btn)

        # 等待页面加载刷新
        time.sleep(2)
        return True

    except TimeoutException:
        print("未找到下一页按钮，可能已经是最后一页。")
        return False
    except Exception as e:
        print(f"翻页出错: {e}")
        return False

def main():
    driver = init_driver()

    try:
        print(f"正在打开系统: {TARGET_URL}")
        driver.get(TARGET_URL)

        # --- 重要提示 ---
        # 如果系统有登录验证，建议先手动登录，或者在代码中加入登录逻辑
        print("请在浏览器中完成登录（如果尚未登录），脚本将在 15 秒后自动开始...")
        time.sleep(15) # 留出时间手动登录

        page_count = 1

        while True:
            print(f"\n--- 正在处理第 {page_count} 页 ---")

            # 1. 处理当前页的所有单据
            has_processed = process_current_page(driver)

            if not has_processed:
                # 如果当前页没处理任何单据，说明这一页已经是干净的，或者没有数据
                # 尝试翻页
                if click_next_page(driver):
                    page_count += 1
                    continue
                else:
                    print("\n所有页面处理完毕，没有发现新的送审任务。")
                    break
            else:
                # 如果处理了单据，通常页面状态会更新（按钮消失），
                # 我们不需要立即翻页，而是继续在 process_current_page 的循环中查找本页剩余按钮。
                # 如果本页确实处理完了，process_current_page 会返回 False，
                # 下一次循环就会触发翻页逻辑。
                pass

    except Exception as e:
        print(f"程序异常终止: {e}")
    finally:
        # 脚本结束后保持浏览器打开，或者根据需要关闭
        # driver.quit()
        print("脚本运行结束。")

if __name__ == "__main__":
    main()