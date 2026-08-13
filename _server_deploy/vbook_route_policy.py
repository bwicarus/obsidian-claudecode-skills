#!/usr/bin/env python3
"""vbook_route_policy.py — 转换层v2 第2步:/pdf 域**运行时全量**路由的 vbook 策略声明。
策略:PAGE=逐页翻译 BOOK_REP=书级 sidecar 归一到代表卷(整组共享一份配置/历史) BOOK_FANIN=列表扇入
MEMBER_REQUIRED=必须真成员/逐卷 JOB_OR_RANGE=任务/流
GLOBAL=与合并无关 EPUB=非PDF域 NONE=静态杂项。**新增路由必须登记**,tests/test_vbook_route_policy 强制。
来源=app.url_map 运行时导出(静态扫描会漏 add_url_rule 动态注册——首跑即实锤截断漏 19+ 条)。"""

ROUTE_POLICY = {
    'pdf_reader.epub_assistant_action': 'EPUB',   # /pdf/api/epub-action
    'pdf_reader.epub_assistant_chat': 'EPUB',   # /pdf/api/epub-assistant
    'pdf_reader.epub_assistant_convo_append': 'EPUB',   # /pdf/api/epub-convo/append
    'pdf_reader.epub_assistant_convo_clear': 'EPUB',   # /pdf/api/epub-convo/clear
    'pdf_reader.epub_assistant_convo_get': 'EPUB',   # /pdf/api/epub-convo
    'pdf_reader.epub_assistant_update_action': 'EPUB',   # /pdf/api/epub-convo/update-action
    'pdf_reader.epub_file': 'EPUB',   # /pdf/epub/file/<sha>/<path:subpath>
    'pdf_reader.epub_view': 'EPUB',   # /pdf/epub/view
    'pdf_reader.html_view': 'EPUB',   # /pdf/html/view
    'pdf_reader.pdf_api_active_reading': 'GLOBAL',   # /pdf/api/active-reading(设备状态,与合并无关;同 reading-pos)
    'pdf_reader.pdf_api_ai_stream_result': 'JOB_OR_RANGE',   # /pdf/api/ai-stream-result
    'pdf_reader.pdf_api_anki_add_cards': 'GLOBAL',   # /pdf/api/anki-add-cards
    'pdf_reader.pdf_api_anki_draft': 'PAGE',   # /pdf/api/anki-draft (精确 PDF 页来源；EPUB 不走 vbook)
    'pdf_reader.pdf_api_asset': 'GLOBAL',   # /pdf/api/asset/<aid>
    'pdf_reader.pdf_api_book_briefs_get': 'BOOK_REP',   # /pdf/api/book-briefs
    'pdf_reader.pdf_api_book_briefs_set': 'BOOK_REP',   # /pdf/api/book-briefs
    'pdf_reader.pdf_api_book_crop_get': 'BOOK_REP',   # /pdf/api/book-crop
    'pdf_reader.pdf_api_book_crop_set': 'MEMBER_REQUIRED',   # /pdf/api/book-crop
    'pdf_reader.pdf_api_book_figures_get': 'MEMBER_REQUIRED',   # /pdf/api/book-figures
    'pdf_reader.pdf_api_book_figures_set': 'MEMBER_REQUIRED',   # /pdf/api/book-figures
    'pdf_reader.pdf_api_book_langs_get': 'BOOK_REP',   # /pdf/api/book-langs
    'pdf_reader.pdf_api_book_langs_set': 'MEMBER_REQUIRED',   # /pdf/api/book-langs
    'pdf_reader.pdf_api_book_meta': 'BOOK_FANIN',   # /pdf/api/book-meta
    'pdf_reader.pdf_api_build_toc': 'MEMBER_REQUIRED',   # /pdf/api/build-toc
    'pdf_reader.pdf_api_build_toc_status': 'MEMBER_REQUIRED',   # /pdf/api/build-toc-status
    'pdf_reader.pdf_api_builtin_tools': 'JOB_OR_RANGE',   # /pdf/api/builtin-tools
    'pdf_reader.pdf_api_cache_stats': 'GLOBAL',   # /pdf/api/cache-stats
    'pdf_reader.pdf_api_card_repository_bootstrap': 'GLOBAL',   # 账户级 Reader 卡仓首次导入，不属于单本/vbook
    'pdf_reader.pdf_api_context_sync': 'GLOBAL',   # /pdf/api/context-sync(全局开关,不涉及具体书)
    'pdf_reader.pdf_api_char_offset_get': 'PAGE',   # /pdf/api/char-offset
    'pdf_reader.pdf_api_char_offset_set': 'PAGE',   # /pdf/api/char-offset
    'pdf_reader.pdf_api_compress_async': 'MEMBER_REQUIRED',   # /pdf/api/compress-async
    'pdf_reader.pdf_api_compress_make': 'MEMBER_REQUIRED',   # /pdf/api/compress-make
    'pdf_reader.pdf_api_compressed_status': 'MEMBER_REQUIRED',   # /pdf/api/compressed-status
    'pdf_reader.pdf_api_delete_pdf': 'MEMBER_REQUIRED',   # /pdf/api/delete-pdf
    'pdf_reader.pdf_api_dict': 'PAGE',   # /pdf/api/dict
    'pdf_reader.pdf_api_dict_jp': 'PAGE',   # /pdf/api/dict-jp
    'pdf_reader.pdf_api_dict_jp_ai': 'PAGE',   # /pdf/api/dict-jp-ai
    'pdf_reader.pdf_api_dict_jp_zh': 'PAGE',   # /pdf/api/dict-jp-zh
    'pdf_reader.pdf_api_dict_quick': 'PAGE',   # /pdf/api/dict-quick
    'pdf_reader.pdf_api_ebook_convert_status': 'MEMBER_REQUIRED',   # /pdf/api/ebook-convert-status
    'pdf_reader.pdf_api_epub_chat': 'EPUB',   # /pdf/api/epub-chat
    'pdf_reader.pdf_api_epub_css': 'EPUB',   # /pdf/api/epub-css
    'pdf_reader.pdf_api_epub_dbg': 'EPUB',   # /pdf/api/epub-dbg
    'pdf_reader.pdf_api_epub_furigana': 'EPUB',   # /pdf/api/epub-furigana
    'pdf_reader.pdf_api_epub_furigana_verify': 'EPUB',   # /pdf/api/epub-furigana-verify
    'pdf_reader.pdf_api_epub_highlights': 'EPUB',   # /pdf/api/epub-highlights
    'pdf_reader.pdf_api_epub_img_describe': 'EPUB',   # /pdf/api/epub-img-describe
    'pdf_reader.pdf_api_epub_ink': 'EPUB',   # /pdf/api/epub-ink
    'pdf_reader.pdf_api_epub_ink_shot': 'EPUB',   # /pdf/api/epub-ink-shot
    'pdf_reader.pdf_api_epub_manifest': 'EPUB',   # /pdf/api/epub-manifest
    'pdf_reader.pdf_api_epub_nodes': 'EPUB',   # /pdf/api/epub-nodes
    'pdf_reader.pdf_api_epub_prefs': 'EPUB',   # /pdf/api/epub-prefs
    'pdf_reader.pdf_api_epub_search': 'EPUB',   # /pdf/api/epub-search
    'pdf_reader.pdf_api_epub_section': 'EPUB',   # /pdf/api/epub-section
    'pdf_reader.pdf_api_epub_to_full': 'EPUB',   # /pdf/api/epub-to-full
    'pdf_reader.pdf_api_epub_tokenize': 'EPUB',   # /pdf/api/epub-tokenize
    'pdf_reader.pdf_api_epub_translate_section': 'EPUB',   # /pdf/api/epub-translate-section
    'pdf_reader.pdf_api_ereader_async': 'MEMBER_REQUIRED',   # /pdf/api/ereader-async
    'pdf_reader.pdf_api_ereader_status': 'MEMBER_REQUIRED',   # /pdf/api/ereader-status
    'pdf_reader.pdf_api_explain': 'PAGE',   # /pdf/api/explain
    'pdf_reader.pdf_api_fav_meta': 'GLOBAL',   # /pdf/api/fav-meta
    'pdf_reader.pdf_api_favorites': 'GLOBAL',   # /pdf/api/favorites
    'pdf_reader.pdf_api_figure_crop': 'PAGE',   # /pdf/api/figure-crop
    'pdf_reader.pdf_api_formula_ocr': 'MEMBER_REQUIRED',   # /pdf/api/formula-ocr
    'pdf_reader.pdf_api_formula_ocr_status': 'PAGE',   # /pdf/api/formula-ocr-status
    'pdf_reader.pdf_api_furigana_verify': 'PAGE',   # /pdf/api/furigana-verify
    'pdf_reader.pdf_api_global_search': 'PAGE',   # /pdf/api/global-search
    'pdf_reader.pdf_api_grammar_analyze': 'BOOK_REP',   # /pdf/api/grammar-analyze
    'pdf_reader.pdf_api_grammar_books': 'GLOBAL',   # /pdf/api/grammar-books
    'pdf_reader.pdf_api_grammar_forget': 'BOOK_REP',   # /pdf/api/grammar-forget
    'pdf_reader.pdf_api_grammar_history': 'BOOK_REP',   # /pdf/api/grammar-history
    'pdf_reader.pdf_api_grammar_history_save': 'BOOK_REP',   # /pdf/api/grammar-history-save
    'pdf_reader.pdf_api_grammar_nodes': 'GLOBAL',   # /pdf/api/grammar-nodes
    'pdf_reader.pdf_api_grammar_stream': 'BOOK_REP',   # /pdf/api/grammar-stream
    'pdf_reader.pdf_api_grammar_tracked': 'BOOK_REP',   # /pdf/api/grammar-tracked
    'pdf_reader.pdf_api_highlight_text': 'PAGE',   # /pdf/api/highlight-text
    'pdf_reader.pdf_api_highlights_create': 'PAGE',   # /pdf/api/highlights
    'pdf_reader.pdf_api_highlights_delete': 'PAGE',   # /pdf/api/highlights
    'pdf_reader.pdf_api_highlights_list': 'PAGE',   # /pdf/api/highlights
    'pdf_reader.pdf_api_highlights_update': 'PAGE',   # /pdf/api/highlights
    'pdf_reader.pdf_api_html_highlights': 'EPUB',   # /pdf/api/html-highlights
    'pdf_reader.pdf_api_img_proxy': 'GLOBAL',   # /pdf/api/img-proxy
    'pdf_reader.pdf_api_ink_list': 'PAGE',   # /pdf/api/ink
    'pdf_reader.pdf_api_ink_save': 'PAGE',   # /pdf/api/ink
    'pdf_reader.pdf_api_job_status': 'JOB_OR_RANGE',   # /pdf/api/job-status
    'pdf_reader.pdf_api_jp_vocab_mark': 'PAGE',   # /pdf/api/jp-vocab-mark
    'pdf_reader.pdf_api_list_pdfs': 'GLOBAL',   # /pdf/api/list-pdfs
    'pdf_reader.pdf_api_library_catalog': 'GLOBAL',   # /pdf/api/library/catalog (原始书库,不经 vbook 重写)
    'pdf_reader.pdf_api_library_download': 'GLOBAL',   # /pdf/api/library/download/<book_id>
    'pdf_reader.pdf_api_library_upload': 'GLOBAL',   # /pdf/api/library/upload
    'pdf_reader.pdf_api_library_ocr_start': 'GLOBAL',   # authenticated opaque library identity
    'pdf_reader.pdf_api_library_ocr_adoption_preview': 'GLOBAL',
    'pdf_reader.pdf_api_library_ocr_adopt': 'GLOBAL',
    'pdf_reader.pdf_api_library_ocr_status': 'GLOBAL',
    'pdf_reader.pdf_api_library_ocr_pause': 'GLOBAL',
    'pdf_reader.pdf_api_library_ocr_resume': 'GLOBAL',
    'pdf_reader.pdf_api_library_ocr_cancel': 'GLOBAL',
    'pdf_reader.pdf_api_library_ocr_retry': 'GLOBAL',
    'pdf_reader.pdf_api_library_ocr_page_chars': 'GLOBAL',
    'pdf_reader.pdf_api_library_ocr_executors': 'GLOBAL',   # executor presence, not a book merge route
    'pdf_reader.pdf_api_library_ocr_worker_claim': 'GLOBAL',   # authenticated opaque worker task
    'pdf_reader.pdf_api_library_ocr_worker_complete': 'GLOBAL',
    'pdf_reader.pdf_api_library_ocr_worker_formulas': 'GLOBAL',
    'pdf_reader.pdf_api_library_ocr_worker_heartbeat': 'GLOBAL',
    'pdf_reader.pdf_api_library_ocr_worker_page': 'GLOBAL',
    'pdf_reader.pdf_api_library_ocr_worker_source': 'GLOBAL',
    'pdf_reader.pdf_api_library_attachments': 'GLOBAL',
    'pdf_reader.pdf_api_library_attachment_download': 'GLOBAL',
    'pdf_reader.pdf_api_library_user_state': 'GLOBAL',
    'pdf_reader.pdf_api_lookup_event': 'PAGE',   # /pdf/api/lookup-event
    'pdf_reader.pdf_api_note_composite': 'PAGE',   # /pdf/api/note-composite
    'pdf_reader.pdf_api_notes': 'PAGE',   # /pdf/api/notes
    'pdf_reader.pdf_api_ocr_selection': 'PAGE',   # /pdf/api/ocr-selection
    'pdf_reader.pdf_api_page_chars': 'PAGE',   # /pdf/api/page-chars
    'pdf_reader.pdf_api_page_brief': 'PAGE',   # /pdf/api/page-brief
    'pdf_reader.pdf_api_page_briefs_all': 'MEMBER_REQUIRED',   # /pdf/api/page-briefs-all
    'pdf_reader.pdf_api_page_figures': 'PAGE',   # /pdf/api/page-figures
    'pdf_reader.pdf_api_page_image': 'PAGE',   # /pdf/api/page-image
    'pdf_reader.pdf_api_page_nodes': 'PAGE',   # /pdf/api/page-nodes
    'pdf_reader.pdf_api_page_offset_set': 'MEMBER_REQUIRED',   # /pdf/api/page-offset
    'pdf_reader.pdf_api_page_overlay': 'PAGE',   # /pdf/api/page-overlay
    'pdf_reader.pdf_api_page_text': 'PAGE',   # /pdf/api/page-text
    'pdf_reader.pdf_api_page_translate': 'PAGE',   # /pdf/api/page-translate
    'pdf_reader.pdf_api_page_vocab_marks': 'PAGE',   # /pdf/api/page-vocab-marks
    'pdf_reader.pdf_api_pdf_insert_page': 'MEMBER_REQUIRED',   # /pdf/api/pdf-insert-page
    'pdf_reader.pdf_api_phrase_mark': 'PAGE',   # /pdf/api/phrase-mark
    'pdf_reader.pdf_api_phrases': 'PAGE',   # /pdf/api/phrases
    'pdf_reader.pdf_api_ping': 'NONE',   # /pdf/api/ping
    'pdf_reader.pdf_api_prefs': 'GLOBAL',   # /pdf/api/prefs
    'pdf_reader.pdf_api_preprocess_active': 'GLOBAL',   # /pdf/api/preprocess-active
    'pdf_reader.pdf_api_preprocess_async': 'MEMBER_REQUIRED',   # /pdf/api/preprocess-async
    'pdf_reader.pdf_api_preprocess_status': 'MEMBER_REQUIRED',   # /pdf/api/preprocess-status
    'pdf_reader.pdf_api_prewarm_async': 'MEMBER_REQUIRED',   # /pdf/api/prewarm-async
    'pdf_reader.pdf_api_prewarm_status': 'BOOK_FANIN',   # /pdf/api/prewarm-status
    'pdf_reader.pdf_api_publish_actions': 'JOB_OR_RANGE',   # /pdf/api/publish-actions
    'pdf_reader.pdf_api_read_dwell': 'PAGE',   # /pdf/api/read-dwell
    'pdf_reader.pdf_api_reading_pos': 'GLOBAL',   # /pdf/api/reading-pos
    'pdf_reader.pdf_api_recipe_delete': 'MEMBER_REQUIRED',   # /pdf/api/recipe-delete
    'pdf_reader.pdf_api_recipe_edit': 'MEMBER_REQUIRED',   # /pdf/api/recipe-edit
    'pdf_reader.pdf_api_recipes': 'GLOBAL',   # /pdf/api/recipes
    'pdf_reader.pdf_api_rename_pdf': 'MEMBER_REQUIRED',   # /pdf/api/rename-pdf
    'pdf_reader.pdf_api_reocr_clear': 'PAGE',   # /pdf/api/reocr-page/clear
    'pdf_reader.pdf_api_reocr_page': 'PAGE',   # /pdf/api/reocr-page
    'pdf_reader.pdf_api_run_attach': 'JOB_OR_RANGE',   # /pdf/api/run-attach
    'pdf_reader.pdf_api_run_event': 'JOB_OR_RANGE',   # /pdf/api/run-event
    'pdf_reader.pdf_api_run_save': 'JOB_OR_RANGE',   # /pdf/api/run-save
    'pdf_reader.pdf_api_run_start': 'JOB_OR_RANGE',   # /pdf/api/run-start
    'pdf_reader.pdf_api_run_status': 'JOB_OR_RANGE',   # /pdf/api/run-status
    'pdf_reader.pdf_api_sandbox': 'JOB_OR_RANGE',   # /pdf/api/sandbox
    'pdf_reader.pdf_api_search': 'BOOK_FANIN',   # /pdf/api/search
    'pdf_reader.pdf_api_sentence_dismiss': 'BOOK_FANIN',   # /pdf/api/sentence-dismiss
    'pdf_reader.pdf_api_sentence_harvest': 'BOOK_FANIN',   # /pdf/api/sentence-cards/harvest
    'pdf_reader.pdf_api_sentence_status': 'BOOK_FANIN',   # /pdf/api/sentence-cards/status
    'pdf_reader.pdf_api_snippets_to': 'PAGE',   # /pdf/api/snippets-to
    'pdf_reader.pdf_api_snippets_to_async': 'PAGE',   # /pdf/api/snippets-to-async
    'pdf_reader.pdf_api_to_note': 'PAGE',   # /pdf/api/to-note
    'pdf_reader.pdf_api_direct_command': 'GLOBAL',   # 无 AI 直接命令(anchor 自带 file,不经合并层重写)
    'pdf_reader.pdf_api_direct_events': 'GLOBAL',    # 失败事件订阅(与具体书无关)
    'pdf_reader.pdf_api_outgoing_drawing': 'GLOBAL', # 绘图版本(按 file+page 查,自己校验)
    'pdf_reader.pdf_api_outgoing_focus': 'GLOBAL',   # 焦点上报(引用自带定位)
    'pdf_reader.pdf_api_outgoing_state': 'GLOBAL',   # 合并视图
    'pdf_reader.pdf_api_outgoing_journal': 'GLOBAL', # 出向事件日志(Windows 拉取源)
    'pdf_reader.pdf_api_turn_ack': 'GLOBAL',   # /pdf/api/turn-ack(前端渲染回执,与具体书无关)
    'pdf_reader.pdf_api_toc_get': 'BOOK_FANIN',   # /pdf/api/toc
    'pdf_reader.pdf_api_toolshot': 'JOB_OR_RANGE',   # /pdf/api/toolshot/<name>
    'pdf_reader.pdf_api_translate': 'PAGE',   # /pdf/api/translate
    'pdf_reader.pdf_api_translate_config': 'GLOBAL',   # /pdf/api/translate-config
    'pdf_reader.pdf_api_translate_sentence': 'BOOK_FANIN',   # /pdf/api/translate-sentence
    'pdf_reader.pdf_api_sync_batch': 'GLOBAL',   # /pdf/api/sync-batch
    'pdf_reader.pdf_api_ui_version': 'GLOBAL',   # /pdf/api/ui-version
    'pdf_reader.pdf_api_upload': 'MEMBER_REQUIRED',
    # PWA 网页阅读器/RBI 已退役；端点暂留作 410/书架/原站兼容跳转。
    # 仍登记为 GLOBAL，避免 vbook 路由审计把兼容端点误判成漏登记。
    'pdf_reader.pdf_api_web_fetch': 'GLOBAL',   # RETIRED /pdf/api/web-fetch → 410
    'pdf_reader.pdf_web_portal': 'GLOBAL',   # RETIRED /pdf/web → /pdf/
    'pdf_reader.pdf_web_proxy': 'GLOBAL',   # RETIRED /pdf/web/proxy → 410
    'pdf_reader.pdf_web_live': 'GLOBAL',    # COMPAT /pdf/web/live → 经校验的原 http(s) URL
    'pdf_reader.pdf_web_frame': 'GLOBAL',        # RETIRED → 410
    'pdf_reader.pdf_web_page_mirror': 'GLOBAL',  # RETIRED → 410
    'pdf_reader.pdf_web_res': 'GLOBAL',          # RETIRED → 410
    'pdf_reader.pdf_web_res_mirror': 'GLOBAL',   # RETIRED → 410
    'pdf_reader.pdf_api_web_translate': 'GLOBAL',  # /pdf/api/web-translate(网页沉浸式翻译:纯文本进出)
    'pdf_reader.pdf_api_web_translate_config': 'GLOBAL',  # /pdf/api/web-translate-config(当前账户的无状态翻译后端能力)
    'pdf_reader.pdf_api_web_vocab': 'GLOBAL',      # /pdf/api/web-vocab(未掌握词判定:只查词库)
    'pdf_reader.pdf_api_web_cookie': 'GLOBAL',     # RETIRED;旧 Cookie 文件只备份保留
    'pdf_reader.pdf_api_web_trcache': 'GLOBAL',    # /pdf/api/web-trcache(网页整页译文预取)
    'pdf_reader.pdf_web_rbi': 'GLOBAL',            # RETIRED → 410
    'pdf_reader.pdf_web_rbi_live': 'GLOBAL',       # RETIRED → 410
    'pdf_reader.pdf_api_userpages': 'PAGE',   # /pdf/api/userpages
    'pdf_reader.pdf_api_video_player_prefs': 'GLOBAL',   # /pdf/api/video-player-prefs
    'pdf_reader.pdf_api_video_subtitles': 'GLOBAL',   # /pdf/api/video-subtitles/<vid>
    'pdf_reader.pdf_api_vocab_anki': 'GLOBAL',   # /pdf/api/vocab-anki
    'pdf_reader.pdf_api_vocab_audio': 'GLOBAL',   # /pdf/api/vocab-audio
    'pdf_reader.pdf_api_vocab_list': 'PAGE',   # /pdf/api/vocab-list
    'pdf_reader.pdf_api_vocab_mark': 'PAGE',   # /pdf/api/vocab-mark
    'pdf_reader.pdf_api_vocab_mastery_map': 'BOOK_FANIN',   # /pdf/api/vocab-mastery-map
    'pdf_reader.pdf_api_entity': 'GLOBAL',   # /pdf/api/entity/<aid>
    'pdf_reader.pdf_api_rbi_ticket': 'GLOBAL',   # RETIRED → 410
    'pdf_reader.pdf_api_review_answer': 'GLOBAL',   # /pdf/api/review-answer
    'pdf_reader.pdf_api_review_queue': 'GLOBAL',   # /pdf/api/review-queue
    'pdf_reader.pdf_fav_icon': 'GLOBAL',   # /pdf/fav/icon
    'pdf_reader.pdf_fav_manifest': 'GLOBAL',   # /pdf/fav/manifest
    'pdf_reader.pdf_fav_open': 'GLOBAL',   # /pdf/fav/open
    'pdf_reader.pdf_fav_view': 'GLOBAL',   # /pdf/fav/view
    'pdf_reader.pdf_file': 'GLOBAL',   # /pdf/file/<path:rel>
    'pdf_reader.pdf_index': 'GLOBAL',   # /pdf/
    'pdf_reader.pdf_reader_events': 'JOB_OR_RANGE',   # /pdf/api/reader-events
    'pdf_reader.pdf_search_page': 'GLOBAL',   # /pdf/search
    'pdf_reader.pdf_sw_js': 'GLOBAL',   # /pdf/sw.js
    'pdf_reader.pdf_tools_page': 'NONE',   # /pdf/tools
    'pdf_reader.pdf_view': 'GLOBAL',   # /pdf/view
    'reader_shared_note.shared_note_get': 'GLOBAL',   # 账户级共享便签,不属于任何书
    'reader_shared_note.shared_note_page': 'GLOBAL',  # 账户级共享便签页面
    'reader_shared_note.shared_note_post': 'GLOBAL',  # 账户级共享便签写入
    'reader_cache_identity': 'GLOBAL',   # /pdf/api/cache-identity
}
