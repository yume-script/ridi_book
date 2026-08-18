(function () {
  "use strict";

  // ------------------------------------------------------------------
  // 설정
  // ------------------------------------------------------------------
  var PLUGIN_ID = "ridi_book";

  // db_type(세션 스코프)을 알아내는 방법이 확실치 않아 여러 경로를 시도한다.
  // 실제 코어가 다른 방식으로 넘겨준다면 이 함수만 고치면 됨.
  function getDbType() {
    try {
      if (window.BookOasisContext && window.BookOasisContext.dbType) {
        return window.BookOasisContext.dbType;
      }
    } catch (e) {}
    try {
      if (document.body && document.body.dataset && document.body.dataset.dbType) {
        return document.body.dataset.dbType;
      }
    } catch (e) {}
    return "general";
  }

  var DATA_URL = "/api/media/dashboard/widgets/" + PLUGIN_ID + "/data?type=" + encodeURIComponent(getDbType());

  // ------------------------------------------------------------------
  // DOM 참조
  // ------------------------------------------------------------------
  var gridEl = document.getElementById("rb-grid");
  var statusEl = document.getElementById("rb-status");
  var countEl = document.getElementById("rb-count");
  var refreshBtn = document.getElementById("rb-refresh-btn");

  var yamlView = document.getElementById("rb-yaml-view");
  var yamlBackBtn = document.getElementById("rb-yaml-back");
  var yamlCopyBtn = document.getElementById("rb-yaml-copy");
  var yamlTitleEl = document.getElementById("rb-yaml-title");
  var yamlContentEl = document.getElementById("rb-yaml-content");

  var currentItems = [];

  // ------------------------------------------------------------------
  // 아주 단순한 클라이언트 사이드 YAML 직렬화 (list/dict/scalar만 지원)
  // 서버가 이미 만들어준 값을 다시 보여주는 용도라 복잡한 케이스는 없음.
  // ------------------------------------------------------------------
  function toYamlValue(v) {
    if (v === null || v === undefined || v === "") return '""';
    var s = String(v).replace(/"/g, '\\"');
    return '"' + s + '"';
  }

  function itemToYaml(item) {
    var lines = [];
    var keys = Object.keys(item);
    for (var i = 0; i < keys.length; i++) {
      var k = keys[i];
      var v = item[k];
      if (Array.isArray(v)) {
        lines.push(k + ":");
        for (var j = 0; j < v.length; j++) {
          lines.push("  - " + toYamlValue(v[j]));
        }
      } else {
        lines.push(k + ": " + toYamlValue(v));
      }
    }
    return lines.join("\n");
  }

  // ------------------------------------------------------------------
  // 렌더링
  // ------------------------------------------------------------------
  function renderGrid(items) {
    currentItems = items || [];
    gridEl.innerHTML = "";

    if (currentItems.length === 0) {
      statusEl.textContent = "검색 결과가 없습니다.";
      countEl.textContent = "(0건)";
      return;
    }

    statusEl.textContent = "";
    countEl.textContent = "(" + currentItems.length + "건)";

    currentItems.forEach(function (item, idx) {
      var card = document.createElement("div");
      card.className = "rb-card";
      card.setAttribute("role", "button");
      card.setAttribute("tabindex", "0");

      var img = document.createElement("img");
      img.className = "rb-card-cover";
      img.loading = "lazy";
      img.src = item.cover || "";
      img.alt = item.title || "";
      img.onerror = function () {
        img.style.visibility = "hidden";
      };

      var titleEl = document.createElement("div");
      titleEl.className = "rb-card-title";
      titleEl.textContent = item.title || "(제목 없음)";

      var metaEl = document.createElement("div");
      metaEl.className = "rb-card-meta";
      var metaParts = [item.author, item.publisher].filter(Boolean);
      metaEl.textContent = metaParts.join(" · ");

      card.appendChild(img);
      card.appendChild(titleEl);
      card.appendChild(metaEl);

      card.addEventListener("click", function () {
        showYaml(idx);
      });
      card.addEventListener("keydown", function (e) {
        if (e.key === "Enter" || e.key === " ") {
          e.preventDefault();
          showYaml(idx);
        }
      });

      gridEl.appendChild(card);
    });
  }

  function showYaml(idx) {
    var item = currentItems[idx];
    if (!item) return;

    yamlTitleEl.textContent = item.title || "";
    yamlContentEl.textContent = itemToYaml(item);

    gridEl.hidden = true;
    yamlView.hidden = false;
  }

  function backToGrid() {
    yamlView.hidden = true;
    gridEl.hidden = false;
  }

  // ------------------------------------------------------------------
  // 데이터 로드
  // ------------------------------------------------------------------
  function loadData() {
    statusEl.textContent = "불러오는 중...";
    countEl.textContent = "(불러오는 중…)";
    gridEl.innerHTML = "";

    fetch(DATA_URL, { credentials: "same-origin" })
      .then(function (resp) {
        return resp.json();
      })
      .then(function (data) {
        if (!data || data.success === false) {
          statusEl.textContent = (data && data.error) || "데이터를 불러오지 못했습니다.";
          countEl.textContent = "";
          return;
        }
        if (data.message && (!data.items || data.items.length === 0)) {
          statusEl.textContent = data.message;
        }
        renderGrid(data.items || []);
      })
      .catch(function (err) {
        statusEl.textContent = "요청 중 오류가 발생했습니다: " + err;
        countEl.textContent = "";
      });
  }

  // ------------------------------------------------------------------
  // 이벤트 바인딩
  // ------------------------------------------------------------------
  if (refreshBtn) refreshBtn.addEventListener("click", loadData);
  if (yamlBackBtn) yamlBackBtn.addEventListener("click", backToGrid);
  if (yamlCopyBtn) {
    yamlCopyBtn.addEventListener("click", function () {
      var text = yamlContentEl.textContent || "";
      if (navigator.clipboard && navigator.clipboard.writeText) {
        navigator.clipboard.writeText(text).then(function () {
          yamlCopyBtn.querySelector("span").textContent = "복사됨!";
          setTimeout(function () {
            yamlCopyBtn.querySelector("span").textContent = "복사";
          }, 1500);
        });
      }
    });
  }

  loadData();
})();
