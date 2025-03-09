import os
import random
import time
import logging
import csv
import json
from datetime import datetime
from abc import ABC, abstractmethod
from typing import Dict, Optional

from selenium import webdriver
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.common.action_chains import ActionChains
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.common.exceptions import TimeoutException, NoSuchElementException
from webdriver_manager.chrome import ChromeDriverManager
from openai import OpenAI
from dotenv import load_dotenv
from rich.console import Console

# Set up Rich console for better error output
console = Console()

load_dotenv()

# Parse multiple page URLs
raw_urls = os.getenv("PAGE_URLS", "")
page_urls = [url.strip() for url in raw_urls.split(",") if url.strip()]

CONFIG = {
    "PAGE_URLS": page_urls,
    # Change 'Write a comment...' to your language in Facebook settings like 'Comment as Nguyen Duy' or 'Viết bình luận...'
    "COMMENT_BOX_XPATH": "//div[contains(@aria-label, 'Write a comment') and @contenteditable='true']",
    "MAX_COMMENTS": 1,
    "MAX_ITERATIONS": 5,
    "DELAYS": {
        "SHORT_MIN": 0.5,
        "SHORT_MAX": 2.0,
        "MEDIUM_MIN": 1,
        "MEDIUM_MAX": 3,
        "LONG_MIN": 5,
        "LONG_MAX": 20,
        "RELOAD_PAUSE": 180,
    },
    "CHROME_PROFILE": "Default",
    "LOG_COMMENTS_TO_CSV": True,  # Whether to log comments to a CSV file
    # Comment source configuration
    "COMMENT_SOURCE": os.getenv("COMMENT_SOURCE", "openai"),  # "openai" or "local"
    "LOCAL_COMMENT_FILE": os.getenv("LOCAL_COMMENT_FILE", "comments.json"),
    "COMMENT_ROTATION": os.getenv(
        "COMMENT_ROTATION", "random"
    ),  # "random" or "sequential"
    "FALLBACK_TO_OPENAI": os.getenv("FALLBACK_TO_OPENAI", "true").lower()
    == "true",  # Whether to fallback to OpenAI if local comment retrieval fails
}

OPENAI_CONFIG = {
    "API_KEY": os.getenv("OPENAI_API_KEY"),
    "MODEL": os.getenv("OPENAI_MODEL"),
    "PROMPT": os.getenv("OPENAI_PROMPT")
    + "Do not include emojis or any introductory phrases or additional text.",
}


def setup_logger():
    """
    Set up comprehensive logging configuration.
    """
    os.makedirs("logs", exist_ok=True)
    log_filename = (
        f'logs/facebook_comment_bot_{datetime.now().strftime("%Y%m%d_%H%M%S")}.log'
    )

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s - %(levelname)s: %(message)s",
        handlers=[
            logging.FileHandler(log_filename, encoding="utf-8"),
            logging.StreamHandler(),
        ],
    )
    return logging.getLogger(__name__)


logger = setup_logger()


# Comment Provider classes
class CommentProvider(ABC):
    """Abstract base class for comment providers."""

    @abstractmethod
    def generate_comment(
        self, post_text: str = "", context: Optional[Dict] = None
    ) -> str:
        """Generate a comment based on post text and context."""
        pass


class OpenAICommentProvider(CommentProvider):
    """Provides comments using OpenAI's API."""

    def __init__(self, client, config):
        """
        Initialize the OpenAI comment provider.

        Args:
            client: The OpenAI client instance
            config: Configuration dictionary for OpenAI
        """
        self.client = client
        self.config = config

    def generate_comment(
        self, post_text: str = "", context: Optional[Dict] = None
    ) -> str:
        """
        Generate a comment using OpenAI based on post text and context.

        Args:
            post_text: The text content of the post
            context: Additional context information (optional)

        Returns:
            str: The generated comment
        """
        try:
            # Create a context-aware prompt if post text is available
            if post_text:
                prompt = f"""
Post content: {post_text}

Write a relevant, friendly, fact-checked comment that responds to this post content.
{self.config["PROMPT"]}
"""
            else:
                prompt = self.config["PROMPT"]

            # Call the OpenAI API
            response = self.client.chat.completions.create(
                model=self.config["MODEL"],
                messages=[{"role": "user", "content": prompt}],
            )
            comment = response.choices[0].message.content.strip()
            return comment
        except Exception as e:
            error_msg = f"OpenAI API error: {e}"
            logging.error(error_msg)
            console.print(f"[bold red]Error:[/bold red] {error_msg}")
            raise


class LocalCommentProvider(CommentProvider):
    """Provides comments from a local JSON file."""

    def __init__(self, config):
        """
        Initialize the local comment provider.

        Args:
            config: Configuration dictionary containing local comment settings
        """
        self.config = config
        self.comments = self._load_comments()
        self.current_index = {}  # Track current index for each category for sequential selection
        self.last_comment_data = None  # Store the last selected comment data

    def _load_comments(self) -> Dict:
        """
        Load comments from the specified JSON file.

        Returns:
            Dict: The loaded comments data
        """
        try:
            with open(self.config["LOCAL_COMMENT_FILE"], "r", encoding="utf-8") as f:
                comments_data = json.load(f)

            # Initialize current index for each category if using sequential rotation
            if self.config["COMMENT_ROTATION"] == "sequential":
                for category in comments_data.get("categories", {}):
                    self.current_index[category] = 0

            logging.info(
                f"Loaded {comments_data.get('metadata', {}).get('total_comments', 0)} comments from {self.config['LOCAL_COMMENT_FILE']}"
            )
            return comments_data
        except (FileNotFoundError, json.JSONDecodeError) as e:
            error_msg = f"Error loading comments file: {e}"
            logging.error(error_msg)
            console.print(f"[bold red]Error:[/bold red] {error_msg}")
            # Return empty structure
            return {"categories": {}, "metadata": {"total_comments": 0}}

    def _save_comments(self) -> None:
        """Save the comments back to the JSON file with updated usage information."""
        try:
            with open(self.config["LOCAL_COMMENT_FILE"], "w", encoding="utf-8") as f:
                json.dump(self.comments, f, indent=4)
            logging.info(f"Updated comments file {self.config['LOCAL_COMMENT_FILE']}")
        except Exception as e:
            error_msg = f"Error saving comments file: {e}"
            logging.error(error_msg)
            console.print(f"[bold red]Error:[/bold red] {error_msg}")

    def _select_category(self, post_text: str) -> str:
        """
        Select the most appropriate category based on post text.

        Args:
            post_text: The text content of the post

        Returns:
            str: The selected category name
        """
        # Default to general if no post text or no match
        if not post_text:
            return "general"

        # Define keywords for each category
        category_keywords = {
            "technology": [
                "tech",
                "technology",
                "digital",
                "software",
                "hardware",
                "app",
                "code",
                "programming",
                "AI",
                "data",
            ],
            "business": [
                "business",
                "market",
                "economy",
                "finance",
                "investment",
                "startup",
                "company",
                "entrepreneur",
            ],
            "questions": [],  # No specific keywords, use for random selection
        }

        # Count keyword matches for each category
        category_scores = {category: 0 for category in category_keywords}
        post_text_lower = post_text.lower()

        for category, keywords in category_keywords.items():
            for keyword in keywords:
                if keyword.lower() in post_text_lower:
                    category_scores[category] += 1

        # Get category with highest score
        max_score = max(category_scores.values()) if category_scores else 0
        matching_categories = [c for c, s in category_scores.items() if s == max_score]

        # If we have keyword matches, use them
        if max_score > 0 and matching_categories:
            selected_category = random.choice(matching_categories)
            logging.info(
                f"Selected comment category '{selected_category}' based on keyword matching"
            )
            return selected_category

        # Add a chance to use questions category even without keywords
        if random.random() < 0.2 and "questions" in self.comments.get("categories", {}):
            return "questions"

        # Fall back to general category
        return "general"

    def _select_comment_from_category(self, category: str) -> Dict:
        """
        Select a comment from the specified category using the configured rotation strategy.

        Args:
            category: The category to select from

        Returns:
            Dict: The selected comment data
        """
        categories = self.comments.get("categories", {})

        # Fall back to general if the category doesn't exist or is empty
        if category not in categories or not categories[category]:
            if (
                category != "general"
                and "general" in categories
                and categories["general"]
            ):
                category = "general"
                logging.info(
                    f"Falling back to 'general' category as '{category}' is unavailable"
                )
            else:
                # If even general doesn't exist, return a default comment
                return {
                    "text": "Thank you for sharing!",
                    "reference": "default_fallback",
                    "tags": ["fallback"],
                    "usage_count": 0,
                }

        comments_in_category = categories[category]

        if self.config["COMMENT_ROTATION"] == "sequential":
            # Sequential selection
            index = self.current_index.get(category, 0)
            comment = comments_in_category[index]

            # Update index for next time
            self.current_index[category] = (index + 1) % len(comments_in_category)
        else:
            # Random selection, weighted by least recently used
            # Simple implementation: if a comment has been used recently, it's less likely to be selected again
            weighted_comments = []
            for comment in comments_in_category:
                # Weight inversely proportional to usage count
                weight = 10 / (comment.get("usage_count", 0) + 1)
                weighted_comments.extend([comment] * int(weight))

            comment = random.choice(
                weighted_comments if weighted_comments else comments_in_category
            )

        return comment

    def generate_comment(
        self, post_text: str = "", context: Optional[Dict] = None
    ) -> str:
        """
        Generate a comment from local storage based on post text and context.

        Args:
            post_text: The text content of the post
            context: Additional context information (optional)

        Returns:
            str: The selected comment
        """
        try:
            # Select appropriate category based on post content
            category = self._select_category(post_text)

            # Get comment from the category
            comment_data = self._select_comment_from_category(category)

            # Store the comment data for later reference
            self.last_comment_data = (
                comment_data.copy()
            )  # Make a copy to avoid modifying the original
            self.last_comment_data["category"] = category  # Add category info

            # Update usage statistics
            if "reference" in comment_data:
                comment_data["usage_count"] = comment_data.get("usage_count", 0) + 1
                comment_data["last_used"] = datetime.now().isoformat()

                # Save updates back to file
                self._save_comments()

                # Log selection
                logging.info(
                    f"Selected local comment '{comment_data['reference']}' from category '{category}'"
                )

            return comment_data["text"]
        except Exception as e:
            error_msg = f"Error selecting local comment: {e}"
            logging.error(error_msg)
            console.print(f"[bold red]Error:[/bold red] {error_msg}")

            # Return a default comment as fallback
            return "Thank you for sharing this content!"


class FacebookAICommentBot:
    def __init__(self, config=None):
        """
        Initialize the Facebook comment bot with configuration.
        """
        self.config = {**CONFIG, **(config or {})}
        self.driver = None
        self.csv_file = None
        self.csv_writer = None
        self.current_url = ""  # Track current URL for context

        # Initialize comment provider based on configuration
        try:
            if self.config["COMMENT_SOURCE"] == "local":
                logging.info("Using local comment provider")
                console.print("[bold blue]Using local comment provider[/bold blue]")
                self.comment_provider = LocalCommentProvider(self.config)
            else:
                logging.info("Using OpenAI comment provider")
                console.print("[bold blue]Using OpenAI comment provider[/bold blue]")
                # Initialize OpenAI client
                try:
                    self.openai_client = OpenAI(api_key=OPENAI_CONFIG["API_KEY"])
                    self.comment_provider = OpenAICommentProvider(
                        self.openai_client, OPENAI_CONFIG
                    )
                except Exception as e:
                    error_msg = f"Failed to initialize OpenAI client: {e}"
                    logging.error(error_msg)
                    console.print(f"[bold red]Error:[/bold red] {error_msg}")
                    raise
        except Exception as e:
            error_msg = f"Failed to initialize comment provider: {e}"
            logging.error(error_msg)
            console.print(f"[bold red]Error:[/bold red] {error_msg}")
            raise

        # Initialize CSV logging if enabled
        if self.config["LOG_COMMENTS_TO_CSV"]:
            self.setup_csv_logging()

    def setup_csv_logging(self):
        """
        Set up CSV logging for comments.
        """
        try:
            os.makedirs("logs", exist_ok=True)
            csv_filename = (
                f'logs/comments_{datetime.now().strftime("%Y%m%d_%H%M%S")}.csv'
            )

            self.csv_file = open(csv_filename, "w", newline="", encoding="utf-8")
            self.csv_writer = csv.writer(self.csv_file)

            # Write header row with expanded columns
            self.csv_writer.writerow(
                [
                    "Timestamp",
                    "Page URL",
                    "Post ID",
                    "Post Text Preview",
                    "Comment",
                    "Comment Source",  # OpenAI or local
                    "Comment Reference",  # For local comments
                    "Category",  # For local comments
                    "Likes",
                    "Replies",
                ]
            )

            logger.info(f"CSV logging set up at {csv_filename}")
        except Exception as e:
            error_msg = f"Failed to set up CSV logging: {e}"
            logger.error(error_msg)
            console.print(f"[bold red]Error:[/bold red] {error_msg}")
            self.config["LOG_COMMENTS_TO_CSV"] = False

    def setup_driver(self):
        """
        Sets up and configures the Selenium WebDriver.
        """
        try:
            import platform

            system = platform.system()

            # Use Safari on macOS as default option
            if system == "Darwin":  # macOS
                try:
                    from selenium.webdriver.safari.options import (
                        Options as SafariOptions,
                    )

                    logger.info("Setting up Safari browser for automation")
                    console.print(
                        "[bold blue]Setting up Safari browser for automation[/bold blue]"
                    )

                    # Enable the developer mode in Safari to allow automation
                    # You may need to manually enable this once through Safari -> Develop -> Allow Remote Automation
                    import subprocess

                    try:
                        subprocess.run(["safaridriver", "--enable"], check=True)
                        logger.info("Enabled Safari remote automation")
                    except Exception as e:
                        logger.warning(
                            f"Could not automatically enable Safari automation: {e}"
                        )
                        console.print(
                            "[bold yellow]Safari automation may need to be manually enabled in Safari -> Develop -> Allow Remote Automation[/bold yellow]"
                        )

                    # Create Safari WebDriver
                    safari_options = SafariOptions()
                    self.driver = webdriver.Safari(options=safari_options)
                    logger.info("Safari driver set up successfully.")
                    console.print(
                        "[bold green]Safari driver set up successfully.[/bold green]"
                    )
                    return
                except Exception as safari_error:
                    logger.error(f"Failed to setup Safari driver: {safari_error}")
                    console.print(
                        f"[bold red]Failed to use Safari: {safari_error}[/bold red]"
                    )
                    console.print(
                        "[bold yellow]Falling back to Chrome setup[/bold yellow]"
                    )
                    # Fall back to Chrome setup

            # Regular Chrome setup as fallback
            chrome_options = Options()
            chrome_options.add_argument("--disable-popup-blocking")
            chrome_options.add_argument("--disable-notifications")
            chrome_options.add_experimental_option(
                "excludeSwitches", ["enable-automation"]
            )
            chrome_options.add_experimental_option("useAutomationExtension", False)

            # Detect OS and set appropriate browser binary location
            if system == "Windows":
                chrome_options.binary_location = (
                    "C:/Program Files/Google/Chrome/Application/chrome.exe"
                )
            elif system == "Darwin":  # macOS
                # Check for Chrome on macOS
                import os

                possible_paths = [
                    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
                    "~/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
                ]

                # Find the first path that exists
                for path in possible_paths:
                    expanded_path = os.path.expanduser(path)
                    if os.path.exists(expanded_path):
                        chrome_options.binary_location = expanded_path
                        break

                # If no Chrome path is found, let Selenium find it automatically
                if (
                    not hasattr(chrome_options, "binary_location")
                    or not chrome_options.binary_location
                ):
                    logger.info(
                        "Chrome binary not explicitly set, letting Selenium detect it automatically."
                    )

            # Create a custom user-data dir
            user_data_dir = os.path.join(os.getcwd(), "chrome_data")
            chrome_options.add_argument(f"--user-data-dir={user_data_dir}")
            chrome_options.add_argument(
                f"--profile-directory={self.config['CHROME_PROFILE']}"
            )

            service = Service(ChromeDriverManager().install())
            self.driver = webdriver.Chrome(service=service, options=chrome_options)
            logger.info("Chrome driver set up successfully.")
            console.print("[bold green]Chrome driver set up successfully.[/bold green]")
        except Exception as e:
            error_msg = f"Failed to setup Chrome Driver: {e}"
            logger.error(error_msg)
            console.print(f"[bold red]Error:[/bold red] {error_msg}")
            raise

    def random_pause(self, min_time=1, max_time=5):
        """
        Pause execution for a random duration between min_time and max_time seconds.
        """
        delay = random.uniform(min_time, max_time)
        time.sleep(delay)
        logger.debug(f"Paused for {delay:.2f} seconds.")

    def human_mouse_jiggle(self, element, moves=2):
        """
        Simulate human-like mouse movements over a given element.

        Args:
            element: The web element to move the mouse over.
            moves: Number of jiggle movements.
        """
        try:
            actions = ActionChains(self.driver)
            actions.move_to_element(element).perform()

            for _ in range(moves):
                x_offset = random.randint(-15, 15)
                y_offset = random.randint(-15, 15)
                actions.move_by_offset(x_offset, y_offset).perform()
                self.random_pause(0.3, 1)

            # Return to the element
            actions.move_to_element(element).perform()
            self.random_pause(0.3, 1)
            logger.debug(f"Performed mouse jiggle with {moves} moves.")
        except Exception as e:
            logger.error(f"Mouse jiggle failed: {e}")

    def human_type(self, element, text):
        """
        Simulate human-like typing into a web element.

        Args:
            element: The web element to type into.
            text: The text to type.
        """
        words = text.split()
        for w_i, word in enumerate(words):
            # Introduce random fake words
            if random.random() < 0.05:
                fake_word = random.choice(["aaa", "zzz", "hmm", "umm", "oops", "wait"])
                for c in fake_word:
                    element.send_keys(c)
                    time.sleep(random.uniform(0.08, 0.35))
                for _ in fake_word:
                    element.send_keys(Keys.BACKSPACE)
                    time.sleep(random.uniform(0.06, 0.25))

            for char in word:
                # Increased chance of typo for more realism
                if random.random() < 0.08:
                    # Get a character close to the intended one on the keyboard
                    keyboard_rows = ["qwertyuiop", "asdfghjkl", "zxcvbnm"]
                    char_row = None
                    char_idx = -1

                    # Find the character in the keyboard layout
                    for row_idx, row in enumerate(keyboard_rows):
                        if char.lower() in row:
                            char_row = row_idx
                            char_idx = row.index(char.lower())
                            break

                    # If found, pick an adjacent key as the typo
                    if char_row is not None and char_idx != -1:
                        row = keyboard_rows[char_row]
                        if char_idx > 0 and char_idx < len(row) - 1:
                            # Character has neighbors on both sides
                            wrong_char = random.choice(
                                [row[char_idx - 1], row[char_idx + 1]]
                            )
                        elif char_idx == 0:
                            # First character in row
                            wrong_char = row[char_idx + 1]
                        else:
                            # Last character in row
                            wrong_char = row[char_idx - 1]
                    else:
                        # Fallback to random character
                        wrong_char = random.choice("abcdefghijklmnopqrstuvwxyz")

                    element.send_keys(wrong_char)
                    time.sleep(random.uniform(0.08, 0.35))
                    element.send_keys(Keys.BACKSPACE)
                    time.sleep(random.uniform(0.06, 0.25))

                # Type the correct character
                element.send_keys(char)
                # More variable typing speed
                time.sleep(random.uniform(0.05, 0.4))

            if w_i < len(words) - 1:
                element.send_keys(" ")
                time.sleep(random.uniform(0.08, 0.3))

            # More random cursor movements
            if random.random() < 0.05:
                # Move cursor around more realistically
                moves = random.randint(1, 3)
                for _ in range(moves):
                    direction = random.choice([Keys.ARROW_LEFT, Keys.ARROW_RIGHT])
                    element.send_keys(direction)
                    time.sleep(random.uniform(0.1, 0.3))

                # Sometimes add/delete a space
                if random.random() < 0.3:
                    if random.random() < 0.5:
                        element.send_keys(" ")
                        time.sleep(random.uniform(0.1, 0.3))
                        element.send_keys(Keys.BACKSPACE)
                    else:
                        element.send_keys(Keys.BACKSPACE)
                        time.sleep(random.uniform(0.1, 0.3))
                        element.send_keys(" ")
                    time.sleep(random.uniform(0.1, 0.3))

        # Occasionally pause before submitting as if reviewing what was typed
        if random.random() < 0.3:
            review_time = random.uniform(1.0, 3.0)
            time.sleep(review_time)
            logger.debug(f"Paused for {review_time:.2f} seconds to review text.")

        self.random_pause(0.5, 1.5)
        logger.debug("Completed human-like typing.")

    def random_scroll(self):
        """
        Scroll up/down randomly to mimic a user's reading or browsing.
        """
        scroll_direction = random.choice(["up", "down"])
        scroll_distance = random.randint(200, 800)

        if scroll_direction == "down":
            self.driver.execute_script(f"window.scrollBy(0, {scroll_distance});")
            logger.debug(f"Scrolling down {scroll_distance} pixels.")
        else:
            self.driver.execute_script(f"window.scrollBy(0, -{scroll_distance});")
            logger.debug(f"Scrolling up {scroll_distance} pixels.")

        self.random_pause(1, 3)

    def random_hover_or_click(self):
        """
        Randomly hover or click on some links or elements on the page to mimic user exploration.
        """
        all_links = self.driver.find_elements(By.TAG_NAME, "a")
        if not all_links:
            return

        if random.random() < 0.5:
            random_link = random.choice(all_links)
            try:
                actions = ActionChains(self.driver)
                actions.move_to_element(random_link).perform()
                logger.debug("Hovering over a random link.")
                self.random_pause(1, 3)

                if random.random() < 0.2:
                    random_link.click()
                    logger.debug("Clicked a random link. Going back in 3 seconds.")
                    time.sleep(3)
                    self.driver.back()
                    self.random_pause(1, 3)
            except Exception as e:
                logger.debug(f"Random hover/click failed: {e}")

    def find_target_post(self):
        """
        Find a target post on the page to comment on.
        Returns the post element or None if no posts are found.
        """
        try:
            # Common Facebook post container selectors
            post_selectors = [
                "//div[contains(@class, 'userContentWrapper')]",  # Classic FB
                "//div[contains(@data-testid, 'post_container')]",  # New FB
                "//div[contains(@role, 'article')]",  # General article role
                "//div[contains(@class, 'feed_story')]",  # Feed stories
            ]

            # Try each selector until we find posts
            posts = []
            for selector in post_selectors:
                posts = self.driver.find_elements(By.XPATH, selector)
                if posts:
                    break

            if not posts:
                logger.warning("No posts found on the page.")
                return None

            # For now, simply return the first post (usually the latest)
            # In a future enhancement, we could analyze engagement metrics
            logger.info(f"Found {len(posts)} posts. Selecting the first one.")
            return posts[0]

        except Exception as e:
            logger.error(f"Error finding target post: {e}")
            return None

    def get_post_text(self, post_element):
        """
        Extract the text content from a post element.

        Args:
            post_element: The post element to extract text from

        Returns:
            str: The extracted text or an empty string if no text is found
        """
        try:
            # Common selectors for post text content
            text_selectors = [
                ".//div[contains(@data-ad-comet-preview, 'message')]",  # New FB
                ".//div[contains(@class, 'userContent')]",  # Classic FB
                ".//span[contains(@class, 'highlightable')]",  # Another common class
                ".//div[contains(@class, 'text_exposed_root')]",  # Exposed text
                ".//div[contains(@dir, 'auto')]",  # General text with auto direction
            ]

            # Try each selector
            for selector in text_selectors:
                try:
                    text_elements = post_element.find_elements(By.XPATH, selector)
                    if text_elements:
                        # Combine text from all matching elements
                        post_text = " ".join(
                            [elem.text for elem in text_elements if elem.text]
                        )
                        if post_text:
                            # Check if there's a "See more" button and click it if found
                            see_more_buttons = post_element.find_elements(
                                By.XPATH, ".//div[contains(text(), 'See more')]"
                            )
                            if see_more_buttons:
                                for button in see_more_buttons:
                                    try:
                                        button.click()
                                        self.random_pause(0.5, 1.5)
                                        # Re-get the text after expanding
                                        post_text = " ".join(
                                            [
                                                elem.text
                                                for elem in post_element.find_elements(
                                                    By.XPATH, selector
                                                )
                                                if elem.text
                                            ]
                                        )
                                    except Exception as e:
                                        logger.debug(
                                            f"Failed to click 'See more' button: {e}"
                                        )

                            logger.info(f"Extracted post text: {post_text[:100]}...")
                            return post_text
                except Exception as e:
                    logger.debug(f"Error with selector {selector}: {e}")
                    continue

            logger.warning("No text found in the post.")
            return ""

        except Exception as e:
            logger.error(f"Error extracting post text: {e}")
            return ""

    def generate_comment(self, post_text="") -> str:
        """
        Generate a comment using the configured comment provider.

        Args:
            post_text: The text content of the post

        Returns:
            str: The generated comment
        """
        try:
            # Create context dictionary with additional information
            context = {"url": self.current_url, "timestamp": datetime.now().isoformat()}

            # Get comment from the provider
            comment = self.comment_provider.generate_comment(
                post_text=post_text, context=context
            )
            logging.info(
                f"Generated comment using {self.config['COMMENT_SOURCE']} provider"
            )

            return comment
        except Exception as e:
            error_msg = f"Failed to generate comment: {e}"
            logging.error(error_msg)
            console.print(f"[bold red]Comment Generation Error:[/bold red] {e}")

            # Try fallback to OpenAI if configured and not already using it
            if (
                self.config["COMMENT_SOURCE"] == "local"
                and self.config["FALLBACK_TO_OPENAI"]
            ):
                try:
                    logging.info("Attempting fallback to OpenAI for comment generation")
                    console.print(
                        "[bold yellow]Falling back to OpenAI for comment generation[/bold yellow]"
                    )

                    # Create OpenAI provider if needed
                    if not hasattr(self, "openai_client"):
                        self.openai_client = OpenAI(api_key=OPENAI_CONFIG["API_KEY"])

                    openai_provider = OpenAICommentProvider(
                        self.openai_client, OPENAI_CONFIG
                    )
                    comment = openai_provider.generate_comment(
                        post_text=post_text, context=context
                    )
                    logging.info("Successfully generated fallback comment using OpenAI")
                    return comment
                except Exception as fallback_error:
                    logging.error(f"OpenAI fallback also failed: {fallback_error}")

            # Default fallback comment if all else fails
            return "Thank you for sharing this content! 😊"

    def get_post_id(self, post_element):
        """
        Attempt to extract a post ID or unique identifier from a post element.

        Args:
            post_element: The post element to extract ID from

        Returns:
            str: The extracted ID or a timestamp-based ID if not found
        """
        try:
            # Try to find data-ft attribute which often contains post ID
            data_ft = post_element.get_attribute("data-ft")
            if data_ft and "top_level_post_id" in data_ft:
                import json

                data = json.loads(data_ft)
                if "top_level_post_id" in data:
                    return data["top_level_post_id"]

            # Try to find data-testid with post ID
            data_testid = post_element.get_attribute("data-testid")
            if data_testid and "post" in data_testid:
                return data_testid

            # Try to find any ID attribute
            post_id = post_element.get_attribute("id")
            if post_id:
                return post_id

            # If all else fails, generate a timestamp-based ID
            return f"post_{int(time.time())}"
        except Exception as e:
            logger.debug(f"Failed to extract post ID: {e}")
            return f"post_{int(time.time())}"

    def post_comment(
        self,
        comment: str,
        comment_count: int,
        post_element=None,
        page_url="",
        post_text="",
        comment_metadata=None,
    ):
        """
        Locate the comment box, click it, and post a comment with "human-like" actions.

        Args:
            comment: The comment text to post
            comment_count: The current comment count
            post_element: The post element to comment on (optional)
            page_url: The URL of the page being processed
            post_text: The text content of the post
            comment_metadata: Additional metadata about the comment
        """
        try:
            # If a post element is provided, search for the comment box within it
            if post_element:
                try:
                    # First try to find the comment box within the post element
                    comment_area = post_element.find_element(
                        By.XPATH,
                        ".//div[contains(@aria-label, 'Write a comment') and @contenteditable='true']",
                    )
                except NoSuchElementException:
                    # If not found, try to click on the comment button first
                    try:
                        comment_button = post_element.find_element(
                            By.XPATH, ".//span[contains(text(), 'Comment')]"
                        )
                        comment_button.click()
                        self.random_pause(1, 2)
                        # Now try to find the comment box again
                        comment_area = post_element.find_element(
                            By.XPATH,
                            ".//div[contains(@aria-label, 'Write a comment') and @contenteditable='true']",
                        )
                    except NoSuchElementException:
                        # If still not found, fall back to the global search
                        logger.warning(
                            "Comment box not found within post, falling back to global search"
                        )
                        comment_area = WebDriverWait(self.driver, 10).until(
                            EC.presence_of_element_located(
                                (By.XPATH, self.config["COMMENT_BOX_XPATH"])
                            )
                        )
            else:
                # Use the global comment box selector
                comment_area = WebDriverWait(self.driver, 10).until(
                    EC.presence_of_element_located(
                        (By.XPATH, self.config["COMMENT_BOX_XPATH"])
                    )
                )

            # Random scroll or random hover before posting the comment
            if random.random() < 0.4:
                self.random_scroll()
            else:
                self.random_hover_or_click()

            # Human-like mouse movements before clicking
            self.human_mouse_jiggle(comment_area, moves=3)

            # Click inside the comment box
            comment_area.click()
            self.random_pause(0.5, 2.0)

            # Human-like typing into the comment box
            self.human_type(comment_area, comment)
            self.random_pause(0.5, 2.0)

            # Submit the comment (press Enter)
            comment_area.send_keys(Keys.RETURN)
            self.random_pause(0.5, 2.0)

            logger.info(f"Comment {comment_count} posted: '{comment}'")

            # Extract post ID and log to CSV
            if post_element and self.config["LOG_COMMENTS_TO_CSV"]:
                post_id = self.get_post_id(post_element)

                # Wait a moment to see if we can detect likes/replies
                self.random_pause(2, 5)

                # Try to find likes/replies (this is a basic implementation)
                likes = 0
                replies = 0
                try:
                    # Look for the newly posted comment
                    comments = post_element.find_elements(
                        By.XPATH, ".//div[contains(@aria-label, 'Comment by')]"
                    )
                    if comments:
                        # Get the most recent comment (likely ours)
                        recent_comment = comments[-1]

                        # Look for like count
                        like_elements = recent_comment.find_elements(
                            By.XPATH,
                            ".//span[contains(text(), 'Like') or contains(text(), 'likes')]",
                        )
                        for elem in like_elements:
                            like_text = elem.text
                            if like_text and any(c.isdigit() for c in like_text):
                                # Extract digits from text
                                likes = int(
                                    "".join(filter(str.isdigit, like_text)) or 0
                                )
                                break

                        # Look for reply count
                        reply_elements = recent_comment.find_elements(
                            By.XPATH,
                            ".//span[contains(text(), 'Reply') or contains(text(), 'replies')]",
                        )
                        for elem in reply_elements:
                            reply_text = elem.text
                            if reply_text and any(c.isdigit() for c in reply_text):
                                # Extract digits from text
                                replies = int(
                                    "".join(filter(str.isdigit, reply_text)) or 0
                                )
                                break
                except Exception as e:
                    logger.debug(f"Failed to extract engagement metrics: {e}")

                # If we don't have comment metadata, create a basic one
                if not comment_metadata:
                    comment_metadata = {
                        "source": self.config["COMMENT_SOURCE"],
                        "reference": "",
                    }

                # Log the comment to CSV
                self.log_comment_to_csv(
                    page_url,
                    post_id,
                    post_text,
                    comment,
                    likes,
                    replies,
                    comment_metadata,
                )

        except TimeoutException:
            logger.warning(
                f"Comment {comment_count} posting timeout - element not found"
            )
            raise
        except NoSuchElementException:
            logger.warning(f"Comment {comment_count} posting element not found")
            raise
        except Exception as e:
            logger.error(
                f"Error during comment posting for comment count {comment_count}: {e}"
            )
            raise

    def log_comment_to_csv(
        self,
        page_url,
        post_id,
        post_text,
        comment,
        likes=0,
        replies=0,
        comment_metadata=None,
    ):
        """
        Log a comment to the CSV file.

        Args:
            page_url: The URL of the page
            post_id: The ID or identifier of the post
            post_text: The text of the post
            comment: The comment text
            likes: Number of likes on the comment (if available)
            replies: Number of replies to the comment (if available)
            comment_metadata: Additional metadata about the comment (source, reference, etc.)
        """
        if not self.config["LOG_COMMENTS_TO_CSV"] or not self.csv_writer:
            return

        try:
            # Truncate post text for preview
            post_text_preview = (
                post_text[:100] + "..." if len(post_text) > 100 else post_text
            )

            # Get comment source and reference
            comment_source = self.config["COMMENT_SOURCE"]
            comment_reference = ""
            comment_category = ""

            # Include metadata if available
            if comment_metadata:
                comment_source = comment_metadata.get("source", comment_source)
                comment_reference = comment_metadata.get("reference", "")
                comment_category = comment_metadata.get("category", "")

            # Write the row
            self.csv_writer.writerow(
                [
                    datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                    page_url,
                    post_id,
                    post_text_preview,
                    comment,
                    comment_source,
                    comment_reference,
                    comment_category,
                    likes,
                    replies,
                ]
            )

            # Flush to ensure data is written
            self.csv_file.flush()

            logger.debug(f"Comment logged to CSV: {comment[:50]}...")
        except Exception as e:
            error_msg = f"Failed to log comment to CSV: {e}"
            logger.error(error_msg)
            console.print(f"[bold red]Error:[/bold red] {error_msg}")

    def run(self):
        """
        Main method to execute the Facebook comment bot with human-like actions.
        """
        try:
            self.setup_driver()

            # Initialize comment counter
            comment_count = 0

            # Iterate through each page URL
            for page_url in self.config["PAGE_URLS"]:
                try:
                    logger.info(f"Processing page URL: {page_url}")
                    console.print(f"[bold blue]Processing page:[/bold blue] {page_url}")

                    # Store the current URL for context
                    self.current_url = page_url

                    self.driver.get(page_url)
                    logger.info(f"Loaded Facebook page URL: {page_url}")

                    # Random pause after loading the page
                    self.random_pause(
                        self.config["DELAYS"]["MEDIUM_MIN"],
                        self.config["DELAYS"]["MEDIUM_MAX"],
                    )

                    # Find a target post to comment on
                    target_post = self.find_target_post()
                    if not target_post:
                        warning_msg = f"No suitable posts found on {page_url}. Moving to next page."
                        logger.warning(warning_msg)
                        console.print(
                            f"[bold yellow]Warning:[/bold yellow] {warning_msg}"
                        )
                        continue

                    logger.info("Found a target post to comment on.")
                    console.print(
                        "[bold green]Found a target post to comment on.[/bold green]"
                    )

                    for i in range(self.config["MAX_ITERATIONS"]):
                        # Stop if we've hit the maximum comment limit
                        if comment_count >= self.config["MAX_COMMENTS"]:
                            logger.info("Max comments reached.")
                            console.print(
                                "[bold green]Max comments reached.[/bold green]"
                            )
                            break

                        self.random_pause(0.5, 2.0)

                        # Occasional "idle time" as if the user is reading or distracted
                        if random.random() < 0.2:
                            idle_time = random.randint(5, 10)
                            logger.debug(f"Idling for {idle_time} seconds.")
                            time.sleep(idle_time)

                        try:
                            # Extract post text
                            post_text = self.get_post_text(target_post)

                            # Generate comment using the comment provider
                            comment = self.generate_comment(post_text)

                            # Create metadata for logging
                            comment_metadata = {
                                "source": self.config["COMMENT_SOURCE"],
                                "reference": "",
                                "category": "",
                            }

                            # For local comments, try to extract reference and category information
                            if self.config["COMMENT_SOURCE"] == "local" and hasattr(
                                self.comment_provider, "last_comment_data"
                            ):
                                last_data = self.comment_provider.last_comment_data
                                if last_data:
                                    comment_metadata["reference"] = last_data.get(
                                        "reference", ""
                                    )
                                    comment_metadata["category"] = last_data.get(
                                        "category", ""
                                    )

                            # Post the comment
                            self.post_comment(
                                comment,
                                comment_count + 1,
                                target_post,
                                page_url,
                                post_text,
                                comment_metadata,
                            )
                            comment_count += 1
                            console.print(
                                f"[bold green]Comment {comment_count} posted successfully.[/bold green]"
                            )
                        except Exception as e:
                            error_msg = f"Iteration {i+1} failed to post comment: {e}"
                            logger.warning(error_msg)
                            console.print(
                                f"[bold yellow]Warning:[/bold yellow] {error_msg}"
                            )

                            # If posting fails, try to find a new target post
                            try:
                                target_post = self.find_target_post()
                                if not target_post:
                                    warning_msg = "Failed to find a new target post. Moving to next page."
                                    logger.warning(warning_msg)
                                    console.print(
                                        f"[bold yellow]Warning:[/bold yellow] {warning_msg}"
                                    )
                                    break
                            except Exception as find_error:
                                error_msg = (
                                    f"Error finding new target post: {find_error}"
                                )
                                logger.error(error_msg)
                                console.print(
                                    f"[bold red]Error:[/bold red] {error_msg}"
                                )
                                break

                        # Every 30 comments, refresh and take a longer pause
                        if comment_count % 30 == 0 and comment_count != 0:
                            logger.info(
                                f"Comment count: {comment_count}. Refreshing page."
                            )
                            console.print(
                                f"[bold blue]Comment count: {comment_count}. Refreshing page.[/bold blue]"
                            )
                            self.driver.refresh()
                            self.random_pause(
                                self.config["DELAYS"]["RELOAD_PAUSE"] - 10,
                                self.config["DELAYS"]["RELOAD_PAUSE"] + 10,
                            )  # Adding some randomness to the pause

                            # After refresh, find a new target post
                            try:
                                target_post = self.find_target_post()
                                if not target_post:
                                    warning_msg = "Failed to find a new target post after refresh. Moving to next page."
                                    logger.warning(warning_msg)
                                    console.print(
                                        f"[bold yellow]Warning:[/bold yellow] {warning_msg}"
                                    )
                                    break
                            except Exception as find_error:
                                error_msg = f"Error finding new target post after refresh: {find_error}"
                                logger.error(error_msg)
                                console.print(
                                    f"[bold red]Error:[/bold red] {error_msg}"
                                )
                                break

                    # After processing a page, take a longer pause before moving to the next one
                    self.random_pause(
                        self.config["DELAYS"]["LONG_MIN"],
                        self.config["DELAYS"]["LONG_MAX"],
                    )

                except Exception as page_error:
                    error_msg = f"Error processing page {page_url}: {page_error}"
                    logger.error(error_msg)
                    console.print(f"[bold red]Error:[/bold red] {error_msg}")
                    # Continue with the next page
                    continue

        except Exception as e:
            error_msg = f"Bot execution failed: {e}"
            logger.critical(error_msg)
            console.print(f"[bold red]Critical Error:[/bold red] {error_msg}")
        finally:
            # Close the CSV file if it's open
            if self.csv_file:
                try:
                    self.csv_file.close()
                    logger.info("CSV log file closed.")
                    console.print("[bold green]CSV log file closed.[/bold green]")
                except Exception as e:
                    error_msg = f"Error closing CSV file: {e}"
                    logger.error(error_msg)
                    console.print(f"[bold red]Error:[/bold red] {error_msg}")

            # Close the browser
            if self.driver:
                self.driver.quit()
                logger.info("Browser closed.")
                console.print("[bold green]Browser closed.[/bold green]")


def main():
    """
    Main function for the Facebook comment bot.
    """
    try:
        import argparse
        import time
        from datetime import datetime, timedelta

        # Parse command line arguments
        parser = argparse.ArgumentParser(description="Facebook AI Comment Bot")
        parser.add_argument(
            "--schedule", type=str, help="Schedule time in HH:MM format (24-hour)"
        )
        parser.add_argument(
            "--interval", type=int, default=0, help="Run every X minutes (0 = run once)"
        )
        args = parser.parse_args()

        # Handle scheduling if requested
        if args.schedule:
            try:
                # Parse the schedule time
                schedule_hour, schedule_minute = map(int, args.schedule.split(":"))

                console.print(
                    f"[bold blue]Bot scheduled to run at {args.schedule}[/bold blue]"
                )

                while True:
                    now = datetime.now()
                    target_time = now.replace(
                        hour=schedule_hour,
                        minute=schedule_minute,
                        second=0,
                        microsecond=0,
                    )

                    # If the target time is in the past, add a day
                    if target_time < now:
                        target_time += timedelta(days=1)

                    # Calculate seconds until the target time
                    wait_seconds = (target_time - now).total_seconds()

                    console.print(
                        f"[bold blue]Waiting until {target_time.strftime('%Y-%m-%d %H:%M:%S')} to run (about {wait_seconds/60:.1f} minutes)[/bold blue]"
                    )

                    # Wait until the scheduled time
                    time.sleep(wait_seconds)

                    # Run the bot
                    console.print("[bold green]Starting scheduled run...[/bold green]")
                    bot = FacebookAICommentBot()
                    bot.run()

                    # If interval is 0, run once and exit
                    if args.interval == 0:
                        break

                    # Otherwise, wait for the specified interval
                    console.print(
                        f"[bold blue]Waiting {args.interval} minutes until next run...[/bold blue]"
                    )
                    time.sleep(args.interval * 60)
            except ValueError:
                console.print(
                    "[bold red]Error: Invalid schedule format. Use HH:MM (24-hour format).[/bold red]"
                )
                return
        else:
            # Run immediately without scheduling
            bot = FacebookAICommentBot()
            bot.run()
    except Exception as e:
        error_msg = f"Bot initialization failed: {e}"
        logger.critical(error_msg)
        console.print(f"[bold red]Critical Error:[/bold red] {error_msg}")


if __name__ == "__main__":
    main()
