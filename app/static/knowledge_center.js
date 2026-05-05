(function () {
  const root = document.getElementById("knowledge-center");
  if (!root || !Array.isArray(window.KNOWLEDGE_ARTICLES)) {
    return;
  }

  const items = Array.from(document.querySelectorAll(".knowledge-list-item"));
  const search = document.getElementById("knowledge-search");
  const category = document.getElementById("knowledge-category");
  const tag = document.getElementById("knowledge-tag");
  const difficulty = document.getElementById("knowledge-difficulty");
  const platform = document.getElementById("knowledge-platform");
  const count = document.getElementById("knowledge-count");
  const empty = document.getElementById("knowledge-empty");
  const favoriteToggle = document.querySelector(".knowledge-favorite-toggle");
  const favoritesNode = document.getElementById("knowledge-favorites");
  const recentsNode = document.getElementById("knowledge-recents");
  const selectedSlug = root.dataset.selectedSlug;

  const readList = (key) => {
    try {
      return JSON.parse(localStorage.getItem(key) || "[]");
    } catch {
      return [];
    }
  };

  const writeList = (key, value) => localStorage.setItem(key, JSON.stringify(value));

  const renderMiniList = (node, slugs, emptyLabel) => {
    if (!node) return;
    node.innerHTML = "";
    if (!slugs.length) {
      node.innerHTML = `<p class="muted small">${emptyLabel}</p>`;
      return;
    }
    slugs.forEach((slug) => {
      const article = window.KNOWLEDGE_ARTICLES.find((item) => item.slug === slug);
      if (!article) return;
      const link = document.createElement("a");
      link.className = "knowledge-mini-link";
      link.href = `/nethunter/${article.slug}`;
      link.textContent = article.title;
      node.appendChild(link);
    });
  };

  const updateLists = () => {
    renderMiniList(favoritesNode, readList("knowledge-favorites"), "No favorites yet.");
    renderMiniList(recentsNode, readList("knowledge-recents"), "No recent articles yet.");
  };

  const addRecent = (slug) => {
    if (!slug) return;
    const next = [slug].concat(readList("knowledge-recents").filter((item) => item !== slug)).slice(0, 8);
    writeList("knowledge-recents", next);
  };

  const applyFilters = () => {
    const q = (search.value || "").trim().toLowerCase();
    const categoryValue = category.value;
    const tagValue = (tag.value || "").toLowerCase();
    const difficultyValue = difficulty.value;
    const platformValue = platform.value;
    let visible = 0;

    items.forEach((item) => {
      const matchesSearch = !q || item.dataset.search.includes(q);
      const matchesCategory = !categoryValue || item.dataset.category === categoryValue;
      const matchesTag = !tagValue || item.dataset.tags.includes(tagValue);
      const matchesDifficulty = !difficultyValue || item.dataset.difficulty === difficultyValue;
      const matchesPlatform =
        platformValue === "all" ||
        item.dataset.platform === platformValue ||
        ((platformValue === "nethunter" || platformValue === "kali") && item.dataset.platform === "both");
      const show = matchesSearch && matchesCategory && matchesTag && matchesDifficulty && matchesPlatform;
      item.classList.toggle("hidden", !show);
      if (show) visible += 1;
    });

    count.textContent = `${visible} article${visible === 1 ? "" : "s"}`;
    empty.classList.toggle("hidden", visible !== 0);
  };

  [search, category, tag, difficulty, platform].forEach((node) => {
    if (!node) return;
    node.addEventListener("input", applyFilters);
    node.addEventListener("change", applyFilters);
  });

  document.querySelectorAll(".copy-command").forEach((button) => {
    button.addEventListener("click", async () => {
      const command = button.dataset.command || "";
      try {
        await navigator.clipboard.writeText(command);
        button.textContent = "Copied";
        window.setTimeout(() => {
          button.textContent = "Copy";
        }, 1200);
      } catch {
        button.textContent = "Copy failed";
      }
    });
  });

  if (favoriteToggle) {
    favoriteToggle.addEventListener("click", () => {
      const slug = favoriteToggle.dataset.slug;
      const favorites = readList("knowledge-favorites");
      const next = favorites.includes(slug) ? favorites.filter((item) => item !== slug) : [slug].concat(favorites).slice(0, 12);
      writeList("knowledge-favorites", next);
      updateLists();
    });
  }

  addRecent(selectedSlug);
  updateLists();
  applyFilters();
})();
