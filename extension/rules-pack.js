/* rules-pack.js — Rule Book 规则包（Tepat 插件层 #1）
 *
 * 来源：Knowledge/settled-rules.md（Bingka Ubi settled rules, 2026-10-02 版）
 * 形态：纯数据 + 纯前端引擎可执行的检查描述。**插件化原则**：检查能力
 * 按包组织，每个包 = {meta, rules[]}，后续新检查能力照此形态加包。
 *
 * 与 server.py 内置 rule_book 的关系：同一套规则的**前端可执行子集**。
 * 需要语料/LLM 的规则不在此包（那些留在 server/agent 侧）。
 * conf 语义沿用规则书：error = 可判错（深标），warn = 需上下文（中标），
 * note = 提醒（浅标），exception = 不该报的（引擎用于抑制其它规则的误报）。
 */
(() => {
  const PACK = {
    meta: {
      id: 'rule-book-v1',
      title: 'Bingka Ubi Rule Book (settled)',
      source: 'Knowledge/settled-rules.md (2026-10-02)',
      version: 3,
    },
    rules: [
      // ── KS-01 Dari vs Daripada ──
      { id: 'KS-01', conf: 'error', note: "lebih … dari → lebih … daripada (perbandingan = KABOS)",
        re: "\\blebih\\s+[^.,;\\n]{0,40}?\\bdari\\b(?!\\s+(segi|sudut|aspek|perspektif)\\b)" },
      { id: 'KS-01', conf: 'error', note: "berasal dari → berasal daripada (asal = KABOS)",
        re: "\\bberasal\\s+dari\\b" },
      { id: 'KS-01', conf: 'note', note: "dari segi/sudut/aspek/perspektif adalah kekecualian tetap (jangan tukar)",
        re: "\\bdari\\s+(segi|sudut|aspek|perspektif)\\b", noflag: true },

      // ── KS-02 Antara ──
      { id: 'KS-02', conf: 'warn', note: "antara X dan Y — dua item biasanya dengan; kira item dahulu",
        re: "\\bantara\\s+[^.,;\\n]{0,60}?\\s+dan\\s" },
      { id: 'KS-02', conf: 'error', note: "antara … dengan (dua item)",
        re: "\\bantara\\s+\\w+(\\s+\\w+){0,4}\\s+dengan\\b", noflag: true },

      // ── KS-03 Berbanding ──
      { id: 'KS-03', conf: 'error', note: "lebih … berbanding (dengan) → lebih … daripada",
        re: "\\blebih\\s+[^.,;\\n]{0,40}?\\bberbanding(\\s+dengan)?\\b" },
      { id: 'KS-03', conf: 'warn', note: "berbanding tanpa dengan — tidak lengkap (tiada lebih/kurang)",
        re: "\\bberbanding\\b(?!\\s+dengan\\b)(?!\\s+daripada\\b)" },

      // ── KS-04 Kepada vs Bagi/Untuk ──
      { id: 'KS-04', conf: 'warn', note: "habis/faedah/ruang + kepada → bagi (beneficiary, bukan penerima)",
        re: "\\b(habis|faedah|ruang|peluang|habis guna)\\s+kepada\\b" },

      // ── KS-05 Pada vs Dalam vs Di ──
      { id: 'KS-05', conf: 'error', note: "pada + jangka masa → dalam (tempoh)",
        re: "\\bpada\\s+(jangka\\s+masa|tempoh|masa\\s+panjang)\\b" },
      { id: 'KS-05', conf: 'error', note: "dalam + titik masa spesifik → pada (bulan/tahun/tarikh)",
        re: "\\bdalam\\s+(bulan|tahun|minggu|hari)\\s+[A-Z\\d]" },
      { id: 'KS-05', conf: 'error', note: "di + saat/masa → pada (masa, bukan tempat)",
        re: "\\bdi\\s+(saat|masa|ketika|waktu)\\s" },

      // ── KP-01 Ialah vs Adalah ──
      { id: 'KP-01', conf: 'warn', note: "adalah + frasa kerja (Adalah dimaklumkan/diserahkan…) — buang pemeri",
        re: "\\bAdalah\\s+(dimaklumkan|diserahkan|dinyatakan|dijelaskan|diharapkan)\\b" },
      { id: 'KP-01', conf: 'error', note: "adalah merupakan — kembar kata pemeri (lihat KL-01)",
        re: "\\b(adalah|ialah)\\s+merupakan\\b" },

      // ── PI-01 / PI-02 Imbuhan ──
      { id: 'PI-01', conf: 'error', note: "mensetujui → menyetujui / bersetuju dengan (realisasi meN- salah)",
        re: "\\bmensetujui\\b" },
      { id: 'PI-02', conf: 'warn', note: "memberikan + objek hidup (pelajar/guru/orang…) → memberi",
        re: "\\bmemberikan\\s+(pelajar|guru|murid|orang|kanak-kanak|peserta|pengguna|ibu|bapa|rakan)\\b" },

      // ── SA-01 Dangling modifier ──
      { id: 'SA-01', conf: 'warn', note: "Setelah/Selepas/semasa/ketika … + pasif: subjek klausa utama mesti pelaku",
        re: "\\b(Setelah|Selepas|Semasa|Ketika)\\s+[^.\\n]{0,60}?,\\s+[^.\\n]{0,30}\\s+\\bdipasang|dibuat|diberi|digunakan\\b" },

      // ── SA-02 Keselarasan ──
      { id: 'SA-02', conf: 'warn', note: "selaras frasa: kata nama terus dalam siri kata kerja (bergotong-royong, X, menyanyi)",
        re: "\\b(bergotong-royong|menyanyi|bermain)[^.\\n]{0,20},\\s+(?!ber|men|mem|me|di)[a-z]+\\s+[a-z]+\\s+dan\\s+(ber|men|mem|me)" },

      // ── KL-01 Kelewahan ──
      { id: 'KL-01', conf: 'error', note: "kembar kata pemeri/hubung: adalah merupakan / ialah merupakan",
        re: "\\b(adalah|ialah)\\s+merupakan\\b" },
      { id: 'KL-01', conf: 'error', note: "kembar kata hubung: tetapi…namun / walau bagaimanapun…tetapi",
        re: "\\b(walau bagaimanapun|tetapi|namun)[^.\\n]{0,40}\\b(tetapi|namun|walau bagaimanapun)\\b" },
      { id: 'KL-01', conf: 'error', note: "ialah ialah / adalah adalah (ganda botoh)",
        re: "\\b(ialah\\s+ialah|adalah\\s+adalah)\\b" },

      // ── KL-02 Penggandaan ──
      { id: 'KL-02', conf: 'error', note: "penanda jamak + kata ganda nama: Sebahagian/beberapa/para … X-X",
        re: "\\b(sebahagian|beberapa|sesetengah|semua|para)\\s+[^.,\\n]{0,20}?([a-z]+)-\\2\\b" },

      // ── IS-01 Terjemahan langsung ──
      { id: 'IS-01', conf: 'error', note: "kerana fakta bahawa → kerana (kalque 'due to the fact that')",
        re: "\\bkerana\\s+fakta\\s+bahawa\\b" },
      { id: 'IS-01', conf: 'error', note: "adalah kerana → ialah kerana / kerana",
        re: "\\badalah\\s+kerana\\b" },

      // ── IS-02 salah makna（词义错——无法正则判断语义，标注典型高频错误对）──
      { id: 'IS-02', conf: 'warn', note: "tali pikat → tali anjing/cawak (pikat = memikat burung) [LR-02]",
        re: "\\btali\\s+pikat\\b" },
      { id: 'IS-02', conf: 'warn', note: "bunyi amaran (nada peranti) → bunyi isyarat [LR-04]",
        re: "\\bbunyi\\s+amaran\\b" },
      { id: 'IS-02', conf: 'warn', note: "nasihat tidak diundang → tidak diminta [LR-05]",
        re: "\\b(nasihat|komen|pendapat|audi)\\s+yang\\s+tidak\\s+diundang\\b" },

      // ── IS-03 Yang mana / di mana ──
      { id: 'IS-03', conf: 'error', note: "yang mana sebagai konjungsi relatif → yang (kalque 'which')",
        re: "\\b[yY]ang\\s+mana\\b(?!\\s+(satu|antara|lebih))" },
      { id: 'IS-03', conf: 'error', note: "di mana sebagai konjungsi (bukan tempat) → susun semula",
        re: "\\b[dD]i\\s+mana\\b(?!\\s+(berada|lokasi|kedudukan|tempat))" },

      // ── IS-04 Kolokasi ──
      { id: 'IS-04', conf: 'error', note: "menggapai untung → meraih/memperoleh untung",
        re: "\\bmenggapai\\s+untung\\b" },
      { id: 'IS-04', conf: 'error', note: "terima kasih di atas → terima kasih atas (di atas = spatial)",
        re: "\\bterima\\s+kasih\\s+di\\s+atas\\b" },
      { id: 'IS-04', conf: 'error', note: "menyambungkan X kepada Y (bukan hidup) → dengan [LR-06]",
        re: "\\b(menyambungkan|menghubungkan)\\s+[^.\\n]{0,30}\\s+kepada\\s+(port|peranti|kabel|bank|router|wifi)\\b" },

      // ── EJ-01 Ejaan ──
      { id: 'EJ-01', conf: 'error', note: "frekuensy → frekuensi",
        re: "\\bfrekuensy\\b" },
      { id: 'EJ-01', conf: 'error', note: "membantuh → membantu",
        re: "\\bmembantuh\\b" },

      // ── LR learned rules（Error patterns）──
      { id: 'LR-03', conf: 'error', note: "lepas + objek langsung → lepaskan (PI-01)",
        re: "\\b[Jj]angan\\s+lepas\\s+(anjing|kucing|burung|ikan|kanak)\\b" },

      // ── NF / LR exceptions（抑制器：命中时建议其它规则静默）──
      { id: 'NF-2', conf: 'exception', note: "antara + 3+ item + dan adalah betul",
        re: "\\bantara\\s+[^.\\n]{0,25},[^.\\n]{0,25},[^.\\n]{0,25}\\s+dan\\b" },
      { id: 'NF-4', conf: 'exception', note: "gugur meN- pada kata kerja aktif (guna/pakai/cari) — jangan tanda",
        re: "\\b(guna|pakai|cari|bili|tanya)\\b", noflag: true },
      { id: 'LR-01', conf: 'exception', note: "minta satu, beri beberapa opsyen — jangan tanda",
        re: "(?s)\\bsatu\\s+(slogan|cadangan|idea)\\b" },
      { id: 'LR-07', conf: 'exception', note: "dengan = beserta (kamera dengan sensor) — jangan tanda",
        re: "\\b(dengan)\\s+(sensor|kamera|kopi|lauk|sambal)\\b", noflag: true },
    ],
  };

  // 挂到扩展命名空间（content.js 通过 window.TEPAT_PACKS 消费）
  (window.TEPAT_PACKS = window.TEPAT_PACKS || []).push(PACK);
})();
