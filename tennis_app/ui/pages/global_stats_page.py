"""
Global Tennis Stats page.

Catalog-style workspace for global records, tournament-type records,
milestones, and quirky tennis statistics.  The page exposes the statistic
registry first; individual rows can later be wired to concrete SQL queries.
"""

from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QSpinBox,
    QListWidget, QListWidgetItem, QSplitter, QFrame, QPushButton,
    QSizePolicy, QDialog, QDialogButtonBox, QScrollArea, QLineEdit,
)
from PySide6.QtCore import Qt, QThread, Signal, QTimer
from PySide6.QtGui import QColor
from PySide6.QtWidgets import QHeaderView

from ..widgets import DataTable, SearchBar, Separator, PillButtonGroup, MultiPillButtonGroup
from ..theme import COLORS, FONTS
from ...core.global_stats_engine import GlobalStatsEngine


class _GlobalStatWorker(QThread):
    """Compute one global-stat leaderboard off the UI thread."""

    data_ready = Signal(str, int, dict)
    error = Signal(str, int, str)

    def __init__(self, db, stat_id, filters, request_id, parent=None):
        super().__init__(parent)
        self._db = db
        self._stat_id = stat_id
        self._filters = dict(filters)
        self._request_id = request_id

    def run(self):
        try:
            result = GlobalStatsEngine(self._db).compute(
                self._stat_id, self._filters, limit=500)
            self.data_ready.emit(self._stat_id, self._request_id, result)
        except Exception as exc:
            self.error.emit(self._stat_id, self._request_id, str(exc))


SECTIONS = [
    {
        "title": "Global Records",
        "description": "Records book for titles, finals, streaks, rounds, ages, rankings, and opponent strength.",
        "filters": "surface, tournament_level, round, era, min_matches, tour",
    },
    {
        "title": "Tournament Type Stats",
        "description": "Focused records for Grand Slams, Masters 1000, ATP/WTA Finals, Olympics, ATP/WTA 500/250, and Challengers.",
        "filters": "tournament_level, tournament, surface, season, era, round",
    },
    {
        "title": "Player Milestones",
        "description": "Career paths, fastest-to-X records, longevity, consistency, and season-by-season milestones.",
        "filters": "player, milestone, season, era, min_matches, tournament_level",
    },
    {
        "title": "Fun Facts",
        "description": "Reddit-style oddities: home slam splits, specialist indexes, points-vs-match conversion, and unusual streaks.",
        "filters": "player, surface, tournament, tournament_level, era, min_matches",
    },
]


def _stat(stat_id, name, category, scope, description, dimensions, formula):
    return {
        "stat_id": stat_id,
        "name": name,
        "category": category,
        "scope": scope,
        "description": description,
        "dimensions": dimensions,
        "formula_pseudocode": formula,
    }


STAT_CATALOG = [
    _stat("most_titles_overall", "Most titles overall", "titles", "career",
          "Classifica dei giocatori con piu titoli vinti in carriera.",
          "player, tournament, tournament_level, surface, season, era",
          "SELECT winner_name, COUNT(*) FROM matches WHERE round='F' GROUP BY winner_name ORDER BY COUNT(*) DESC"),
    _stat("most_titles_by_level", "Most titles by level", "titles", "tournament_type",
          "Titoli vinti per livello: GS, M1000, ATPF, 500, 250, Olympics.",
          "player, tournament_level, tournament, surface, season, era",
          "filter finals by tournament_level; count titles per winner_name, tournament_level"),
    _stat("most_titles_by_surface", "Most titles by surface", "titles", "surface",
          "Titoli vinti separati per superficie.",
          "player, surface, tournament_level, season, era",
          "filter round='F'; group by winner_name, surface"),
    _stat("title_streak_overall", "Longest title streak", "titles", "all_time",
          "Serie piu lunga di tornei consecutivi vinti dal giocatore tra quelli disputati.",
          "player, tournament, tournament_level, surface, season, era",
          "build player tournament results ordered by date; count consecutive champion results"),
    _stat("title_streak_by_level", "Longest title streak by level", "titles", "tournament_type",
          "Serie piu lunga di titoli consecutivi nello stesso livello di torneo.",
          "player, tournament_level, season, era",
          "build title_streak where tournament_level = selected_level"),
    _stat("most_finals_overall", "Most finals reached", "titles", "career",
          "Finali raggiunte in carriera, includendo finali vinte e perse.",
          "player, tournament, tournament_level, surface, season, era",
          "SELECT player, COUNT(*) FROM finalists(round='F') GROUP BY player"),
    _stat("most_sf_qf_overall", "Most SF/QF reached", "rounds", "career",
          "Presenze in semifinali o quarti, filtrabili per livello e superficie.",
          "player, round, tournament_level, surface, season, era",
          "count player tournaments where best_round_rank <= selected_round_rank"),
    _stat("finals_win_pct", "Finals win percentage", "titles", "career",
          "Percentuale di finali vinte con soglia minima di finali giocate.",
          "player, tournament_level, surface, era, min_matches",
          "titles / finals_reached where finals_reached >= min_matches"),
    _stat("slam_boxset", "Career Grand Slam boxset", "titles", "tournament_type",
          "Giocatori che hanno vinto Australian Open, Roland Garros, Wimbledon e US Open.",
          "player, tournament, tournament_level, era",
          "find winners with COUNT(DISTINCT slam_name)=4"),
    _stat("golden_masters", "Career Golden Masters", "titles", "tournament_type",
          "Giocatori che hanno vinto tutti i Masters 1000 attivi nel set selezionato.",
          "player, tournament, tournament_level, era",
          "find winners where DISTINCT m1000_tournament_count = required_m1000_count"),
    _stat("win_streak_overall", "Longest win streak", "streaks", "all_time",
          "Striscia piu lunga di vittorie consecutive in partite ATP/WTA.",
          "player, tournament_level, surface, season, era",
          "order matches by date; increment streak on win, reset on loss"),
    _stat("win_streak_by_level", "Longest win streak by level", "streaks", "tournament_type",
          "Striscia di vittorie consecutive filtrata per GS, M1000, 500, 250 o Finals.",
          "player, tournament_level, season, era",
          "apply tournament_level filter before streak calculation"),
    _stat("win_streak_by_surface", "Longest win streak by surface", "streaks", "surface",
          "Serie piu lunga di vittorie consecutive su una superficie.",
          "player, surface, tournament_level, season, era",
          "apply surface filter before streak calculation"),
    _stat("loss_streak_overall", "Longest loss streak", "streaks", "all_time",
          "Striscia piu lunga di sconfitte consecutive in partite ATP/WTA.",
          "player, tournament_level, surface, season, era",
          "order matches by date; increment streak on loss, reset on win"),
    _stat("set_streak", "Longest set streak", "streaks", "all_time",
          "Serie piu lunga di set vinti consecutivi nelle partite del giocatore.",
          "player, tournament_level, surface, season, era",
          "flatten set-scores per player ordered by match date; count consecutive sets won"),
    _stat("round_streak_slam_sf_f", "Consecutive Slam SF/F", "rounds", "round",
          "Numero massimo di Slam consecutivi con almeno SF o finale raggiunta.",
          "player, tournament_level, round, season, era",
          "build slam appearances ordered by slam_date; count consecutive best_round <= SF/F"),
    _stat("round_streak_m1000_sf_f", "Consecutive Masters SF/F", "rounds", "round",
          "Serie di Masters 1000 consecutivi con almeno SF o finale.",
          "player, tournament_level, round, season, era",
          "same as round_streak_slam_sf_f where tournament_level='M'"),
    _stat("deep_run_streak", "Consecutive deep runs", "rounds", "career",
          "Tornei consecutivi con almeno QF/SF raggiunta.",
          "player, round, tournament_level, surface, season, era",
          "order player tournament results; count consecutive best_round <= threshold_round"),
    _stat("most_entries_by_level", "Most entries by level", "participation", "tournament_type",
          "Partecipazioni totali in un livello di torneo.",
          "player, tournament_level, season, era",
          "COUNT(DISTINCT tourney_id, year) per player filtered by tournament_level"),
    _stat("most_matches_won_by_level", "Most wins by level", "wins", "tournament_type",
          "Partite vinte in un livello specifico, per esempio Grand Slam o Masters 1000.",
          "player, tournament_level, surface, season, era",
          "COUNT(*) where winner_name=player and tournament_level=selected_level"),
    _stat("best_win_pct_by_level", "Best win percentage by level", "wins", "tournament_type",
          "Miglior win% per livello con soglia minima di match.",
          "player, tournament_level, surface, era, min_matches",
          "wins / matches where matches >= min_matches grouped by player"),
    _stat("longest_gap_between_titles", "Longest gap between titles", "titles", "career",
          "Intervallo piu lungo tra due titoli dello stesso giocatore.",
          "player, tournament_level, surface, season, era",
          "sort title dates per player; compute max(date_n - date_n_minus_1)"),
    _stat("seasons_with_title", "Seasons with a title", "titles", "season",
          "Numero di stagioni diverse con almeno un titolo.",
          "player, season, tournament_level, surface, era",
          "COUNT(DISTINCT season) from finals won grouped by winner_name"),
    _stat("consecutive_seasons_with_title", "Consecutive title seasons", "titles", "season",
          "Serie piu lunga di stagioni consecutive con almeno un titolo.",
          "player, season, tournament_level, surface, era",
          "build seasons_with_title per player; count consecutive years"),
    _stat("youngest_title_winner", "Youngest title winner", "age", "tournament_type",
          "Vincitore piu giovane di un titolo, filtrabile per livello.",
          "player, tournament_level, tournament, surface, season, era",
          "join players dob; age_on(final_date); MIN(age) among finals winners"),
    _stat("oldest_title_winner", "Oldest title winner", "age", "tournament_type",
          "Vincitore piu anziano di un titolo, filtrabile per livello.",
          "player, tournament_level, tournament, surface, season, era",
          "join players dob; age_on(final_date); MAX(age) among finals winners"),
    _stat("youngest_main_draw_player", "Youngest main draw player", "age", "all_time",
          "Giocatore piu giovane apparso nel main draw.",
          "player, tournament_level, tournament, surface, season, era",
          "age_on(first_main_draw_match_date); exclude qualifying rounds"),
    _stat("oldest_main_draw_player", "Oldest main draw player", "age", "all_time",
          "Giocatore piu anziano apparso nel main draw.",
          "player, tournament_level, tournament, surface, season, era",
          "age_on(last_main_draw_match_date); exclude qualifying rounds"),
    _stat("career_length", "Career length", "age", "career",
          "Durata tra primo e ultimo match registrato.",
          "player, tournament_level, surface, era",
          "MAX(match_date) - MIN(match_date) grouped by player"),
    _stat("wins_vs_top10", "Wins vs Top 10", "opponent", "career",
          "Vittorie contro avversari classificati top 10 al momento del match.",
          "player, opponent_rank, tournament_level, surface, season, era",
          "COUNT(*) where winner=player and loser_rank <= 10"),
    _stat("win_pct_vs_top10", "Win percentage vs Top 10", "opponent", "career",
          "Win% contro top 10 con soglia minima di match.",
          "player, opponent_rank, tournament_level, surface, era, min_matches",
          "wins_vs_top10 / matches_vs_top10 where matches_vs_top10 >= min_matches"),
    _stat("wins_vs_top5", "Wins vs Top 5", "opponent", "career",
          "Vittorie contro top 5.",
          "player, opponent_rank, tournament_level, surface, season, era",
          "COUNT(*) where winner=player and loser_rank <= 5"),
    _stat("wins_vs_top3", "Wins vs Top 3", "opponent", "career",
          "Vittorie contro top 3.",
          "player, opponent_rank, tournament_level, surface, season, era",
          "COUNT(*) where winner=player and loser_rank <= 3"),
    _stat("best_win_pct_vs_top10", "Best win percentage vs Top 10", "opponent", "career",
          "Miglior percentuale vittorie contro top 10 con soglia minima.",
          "player, opponent_rank, tournament_level, surface, era, min_matches",
          "rank win_pct_vs_top10 where matches_vs_top10 >= min_matches"),
    _stat("best_record_as_top1", "Best record as No. 1", "ranking", "career",
          "Record W-L e win% quando il giocatore era numero 1.",
          "player, player_rank, tournament_level, surface, season, era",
          "matches where player_rank=1; compute wins, losses, win_pct"),
    _stat("lowest_ranked_win_vs_top5", "Lowest ranked players to beat a Top 5", "ranking", "all_time",
          "Giocatori con ranking piu basso che hanno battuto un Top 5.",
          "player, player_rank, opponent_rank, tournament_level, surface, season, era",
          "winner_rank DESC where winner_rank > 5 and loser_rank <= 5"),
    _stat("lowest_ranked_win_vs_top10", "Lowest ranked players to beat a Top 10", "ranking", "all_time",
          "Giocatori con ranking piu basso che hanno battuto un Top 10.",
          "player, player_rank, opponent_rank, tournament_level, surface, season, era",
          "winner_rank DESC where winner_rank > 10 and loser_rank <= 10"),
    _stat("streak_weeks_at_no1", "Consecutive weeks at No. 1", "ranking", "all_time",
          "Serie piu lunga di settimane consecutive al numero 1.",
          "player, ranking_date, season, era",
          "order ranking weeks; count consecutive rows where rank=1"),
    _stat("streak_weeks_top10", "Consecutive weeks in Top 10", "ranking", "all_time",
          "Serie piu lunga di settimane consecutive in top 10.",
          "player, ranking_date, season, era",
          "order ranking weeks; count consecutive rows where rank <= 10"),
    _stat("career_win_pct_overall", "Career match win percentage", "wins", "career",
          "Percentuale vittorie match in carriera.",
          "player, tournament_level, surface, season, era, min_matches",
          "wins / total_matches grouped by player where total_matches >= min_matches"),
    _stat("career_points_pct_big3_style", "Career points won percentage", "performance", "career",
          "Percentuale punti vinti in carriera quando i dati punto/serve sono disponibili.",
          "player, tournament_level, surface, season, era, min_matches",
          "SUM(points_won) / SUM(total_points) from extended_stats grouped by player"),
    _stat("surface_specialist_index", "Surface specialist index", "performance", "surface",
          "Differenza tra win% su una superficie e win% complessiva del giocatore.",
          "player, surface, tournament_level, era, min_matches",
          "surface_win_pct - overall_win_pct where surface_matches >= min_matches"),
    _stat("slam_dominance_index", "Slam dominance index", "performance", "tournament_type",
          "Indice di dominanza in uno Slam specifico: titoli, finali, win%, set dominance.",
          "player, tournament, tournament_level, surface, era, min_matches",
          "weighted_score(titles, finals, win_pct, straight_set_win_pct) per slam"),
    _stat("fastest_to_100_wins", "Fastest to 100 wins", "milestone", "career",
          "Giocatori che raggiungono 100 vittorie nel minor numero di match giocati.",
          "player, milestone, tournament_level, surface, era",
          "order matches per player; find match_index where cumulative_wins = 100"),
    _stat("fastest_to_50_wins", "Fastest to 50 wins", "milestone", "career",
          "Versione 50 vittorie, utile per confrontare inizi carriera.",
          "player, milestone, tournament_level, surface, era",
          "find match_index where cumulative_wins = 50"),
    _stat("fastest_to_150_wins", "Fastest to 150 wins", "milestone", "career",
          "Versione 150 vittorie, piu stabile per confronti storici.",
          "player, milestone, tournament_level, surface, era",
          "find match_index where cumulative_wins = 150"),
    _stat("most_wins_after_n_matches", "Most wins after N matches", "milestone", "career",
          "Numero massimo di vittorie dopo N match di carriera.",
          "player, milestone, tournament_level, surface, era",
          "take first N matches per player; count wins"),
    _stat("home_slam_performance", "Home Slam performance", "fun", "tournament_type",
          "Rendimento nello Slam di casa rispetto agli altri Slam.",
          "player, tournament, tournament_level, country, era, min_matches",
          "map player_ioc to home_slam; compare home_slam_win_pct vs other_slam_win_pct"),
    _stat("aces_milestones", "Aces milestones", "milestone", "career",
          "Giocatori che raggiungono soglie di ace come 1000, 5000 o 10000.",
          "player, milestone, tournament_level, surface, season, era",
          "cumulative SUM(aces) by match; find first date crossing milestone"),
    _stat("most_bagels_given", "Most bagels given", "fun", "career",
          "Giocatori con piu set vinti 6-0.",
          "player, tournament_level, surface, season, era",
          "parse score; count sets where player_games=6 and opponent_games=0"),
    _stat("most_bagels_received", "Most bagels received", "fun", "career",
          "Giocatori con piu set persi 0-6.",
          "player, tournament_level, surface, season, era",
          "parse score; count sets where player_games=0 and opponent_games=6"),
    _stat("five_set_win_pct", "Five-set win percentage", "performance", "round",
          "Miglior win% nei match al quinto set con soglia minima.",
          "player, tournament_level, round, surface, era, min_matches",
          "filter total_sets=5; wins / matches grouped by player"),
    _stat("deciding_set_win_pct", "Deciding-set win percentage", "performance", "round",
          "Miglior win% nei match arrivati al set decisivo.",
          "player, tournament_level, round, surface, era, min_matches",
          "filter deciding_set=true; wins / matches grouped by player"),
    _stat("tie_break_win_pct", "Tiebreak win percentage", "performance", "career",
          "Percentuale tiebreak vinti con soglia minima di tiebreak giocati.",
          "player, tournament_level, surface, season, era, min_matches",
          "parse score; tiebreaks_won / tiebreaks_played grouped by player"),
    _stat("qualifying_to_title_runs", "Qualifying to title runs", "fun", "tournament_type",
          "Titoli vinti partendo dalle qualificazioni.",
          "player, tournament, tournament_level, surface, season, era",
          "find champions with same event qualifying wins before main draw"),
    _stat("lucky_loser_deep_runs", "Lucky loser deep runs", "fun", "tournament_type",
          "Migliori risultati ottenuti da lucky loser, se il dato e disponibile.",
          "player, tournament, tournament_level, round, season, era",
          "filter entry='LL'; rank by best_round"),
    _stat("same_tournament_title_streak", "Same tournament title streak", "titles", "tournament_type",
          "Titoli consecutivi nello stesso torneo.",
          "player, tournament, tournament_level, surface, season, era",
          "group titles by player,tournament; count consecutive seasons"),
    _stat("most_wins_at_single_tournament", "Most wins at one tournament", "wins", "tournament_type",
          "Numero massimo di vittorie in un singolo torneo.",
          "player, tournament, tournament_level, surface, era",
          "COUNT(wins) grouped by player,tournament"),
    _stat("best_single_season_win_pct", "Best single-season win percentage", "season", "season",
          "Miglior win% in una stagione con soglia minima di match.",
          "player, season, tournament_level, surface, era, min_matches",
          "wins / matches grouped by player,season where matches >= min_matches"),
    _stat("most_titles_single_season", "Most titles in a season", "titles", "season",
          "Numero massimo di titoli vinti in una singola stagione.",
          "player, season, tournament_level, surface, era",
          "COUNT(final wins) grouped by player,season"),
    _stat("most_finals_single_season", "Most finals in a season", "titles", "season",
          "Numero massimo di finali raggiunte in una stagione.",
          "player, season, tournament_level, surface, era",
          "COUNT(final appearances) grouped by player,season"),
    _stat("top10_wins_single_season", "Top 10 wins in a season", "opponent", "season",
          "Vittorie contro top 10 in una singola stagione.",
          "player, opponent_rank, season, tournament_level, surface, era",
          "COUNT(*) where winner=player and loser_rank <= 10 grouped by player,season"),
    _stat("upset_wins_by_rank_gap", "Biggest upset wins by rank gap", "ranking", "all_time",
          "Vittorie con maggiore differenza ranking tra vincitore e sconfitto.",
          "player, opponent_rank, player_rank, tournament_level, surface, era",
          "rank_gap = winner_rank - loser_rank; keep winner_rank > loser_rank; order DESC"),
    _stat("losses_from_match_points", "Losses from match points", "fun", "career",
          "Sconfitte dopo match point avuti, se i dati punto-per-punto sono disponibili.",
          "player, tournament_level, surface, season, era",
          "from point_by_point; detect player had match_point before losing match"),
    _stat("straight_set_title_runs", "Straight-set title runs", "performance", "tournament_type",
          "Titoli vinti senza perdere set nel torneo.",
          "player, tournament, tournament_level, surface, season, era",
          "for champion tournament run, verify sets_lost=0 across all matches"),
    _stat("no_tiebreak_title_runs", "No-tiebreak title runs", "fun", "tournament_type",
          "Titoli vinti senza giocare tiebreak nel torneo.",
          "player, tournament, tournament_level, surface, season, era",
          "for champion tournament run, verify tiebreaks_played=0"),
    _stat("longest_matches_by_time", "Longest matches", "fun", "all_time",
          "Le partite piu lunghe per durata in minuti.",
          "player, tournament_level, surface, season, era",
          "SELECT *, minutes FROM matches ORDER BY minutes DESC"),
    _stat("fastest_matches_by_time", "Fastest matches", "fun", "all_time",
          "Le partite piu veloci per durata in minuti (min 3 set o 1 ora).",
          "player, tournament_level, surface, season, era",
          "SELECT *, minutes FROM matches WHERE minutes > 0 ORDER BY minutes ASC"),
    _stat("fewest_games_won_in_win", "Fewest games won by loser", "fun", "all_time",
          "Partite dove il perdente ha vinto il minor numero di giochi.",
          "player, tournament_level, surface, season, era",
          "parse score; min(games_lost) among losers"),
    _stat("finals_least_breaks_conceded", "Finals won with fewest breaks conceded", "fun", "all_time",
          "Finali vinte subendo il minor numero di break nel proprio turno di servizio.",
          "player, tournament_level, surface, season, era",
          "SELECT winner, w_bpFaced - w_bpSaved AS breaks_conceded FROM matches WHERE round='F' ORDER BY breaks_conceded ASC"),
]


CATEGORY_GROUPS = {
    "All": None,
    "Titles": {"titles"},
    "Wins": {"wins", "performance"},
    "Streaks": {"streaks", "rounds"},
    "Rankings": {"ranking", "opponent"},
    "Ages": {"age"},
    "Milestones": {"milestone", "participation", "season"},
    "Fun": {"fun"},
}


class GlobalStatsPage(QWidget):
    """Simple records browser for global tennis leaderboards."""

    _RESULT_COLUMNS = [
        ("Rank", 50),
        ("Player", 200),
        ("Value", 80),
        ("Detail", 220),
        ("Sets W-L", 90),
        ("Breaks", 90),
    ]

    def __init__(self, db, parent=None):
        super().__init__(parent)
        self.db = db
        self._category = "All"
        self._filtered_stats = []
        self._current_stat = None
        self._worker = None
        self._streak_meta = []
        self._all_rows = []
        self._current_page = 1
        self._request_id = 0
        self._compute_timer = QTimer(self)
        self._compute_timer.setSingleShot(True)
        self._compute_timer.setInterval(180)
        self._compute_timer.timeout.connect(self._load_current_stat)
        self._build_ui()
        self._apply_filters()

    def _build_ui(self):
        self.setStyleSheet(self._page_stylesheet())

        root = QVBoxLayout(self)
        root.setContentsMargins(24, 20, 24, 20)
        root.setSpacing(10)

        header = QLabel("🌐 Global Tennis Stats")
        header.setObjectName("headerLabel")
        root.addWidget(header)

        self.summary_label = QLabel()
        self.summary_label.setVisible(False)

        self._records_panel = QFrame()
        self._records_panel.setObjectName("recordListPanel")
        list_layout = QVBoxLayout(self._records_panel)
        list_layout.setContentsMargins(10, 10, 10, 10)
        list_layout.setSpacing(6)

        records_label = QLabel("Records")
        records_label.setObjectName("sectionLabel")
        list_layout.addWidget(records_label)

        self.stat_count_label = QLabel()
        self.stat_count_label.setObjectName("dimLabel")
        list_layout.addWidget(self.stat_count_label)

        self.stat_list = QListWidget()
        self.stat_list.setObjectName("globalStatList")
        self.stat_list.currentRowChanged.connect(self._on_stat_selected)
        list_layout.addWidget(self.stat_list, 1)

        category_row = QHBoxLayout()
        category_row.setSpacing(10)
        category_row.addWidget(self._label("Category"))
        self.category_pills = PillButtonGroup(list(CATEGORY_GROUPS.keys()), "All")
        self.category_pills.changed.connect(self._on_category_changed)
        category_row.addWidget(self.category_pills, 1)

        category_row.addWidget(self._label("Player"))
        self.player_filter_edit = QLineEdit()
        self.player_filter_edit.setPlaceholderText("Filter by player…")
        self.player_filter_edit.setFixedWidth(180)
        self.player_filter_edit.setStyleSheet(
            f"background-color: {COLORS['bg_card']};"
            f" color: {COLORS['text']};"
            f" border: 1px solid {COLORS['border']};"
            f" border-radius: 4px; padding: 4px 8px;"
        )
        self.player_filter_edit.textChanged.connect(self._apply_player_filter)
        category_row.addWidget(self.player_filter_edit)
        root.addLayout(category_row)

        filters = QFrame()
        filters.setObjectName("globalFilters")
        filters_layout = QVBoxLayout(filters)
        filters_layout.setContentsMargins(12, 8, 12, 10)
        filters_layout.setSpacing(8)

        filters_header = QHBoxLayout()
        filters_header.setContentsMargins(0, 0, 0, 0)
        filters_title = QLabel("Filters")
        filters_title.setObjectName("sectionLabel")
        filters_header.addWidget(filters_title)
        filters_header.addStretch()
        self.filter_toggle = QPushButton("Hide filters")
        self.filter_toggle.setObjectName("accentBtn")
        self.filter_toggle.clicked.connect(self._toggle_filters)
        filters_header.addWidget(self.filter_toggle)
        filters_layout.addLayout(filters_header)

        self.filters_body = QWidget()
        body_layout = QVBoxLayout(self.filters_body)
        body_layout.setContentsMargins(0, 0, 0, 0)
        body_layout.setSpacing(8)

        search_row = QHBoxLayout()
        search_row.setContentsMargins(0, 0, 0, 0)
        search_row.setSpacing(8)
        self.search_bar = SearchBar("Search records...")
        self.search_bar.searched.connect(lambda _: self._apply_filters())
        self.search_bar.line_edit.textChanged.connect(lambda _: self._apply_filters())
        search_row.addWidget(self.search_bar, 1)
        body_layout.addLayout(search_row)

        self.tour_pills = self._filter_pills(["All", "ATP", "WTA"])
        self.level_pills = self._filter_pills([
            "All", "Grand Slam", "Masters 1000", "ATP/WTA Finals",
            "Olympics", "ATP/WTA 500", "ATP/WTA 250", "Challenger",
        ])
        self.surface_pills = self._filter_pills(["All", "Hard", "Clay", "Grass", "Carpet"])
        self.era_pills = self._filter_pills(["All-time", "Open Era", "2000s", "2010s", "2020s"])
        self.round_pills = self._filter_pills([
            "All", "F", "SF", "QF", "R16", "R32", "R64", "R128", "RR", "Q3", "Q2", "Q1",
        ])

        # Default selections: all levels except Challenger; all rounds except qualifiers
        self.level_pills.set_values([
            "Grand Slam", "Masters 1000", "ATP/WTA Finals",
            "Olympics", "ATP/WTA 500", "ATP/WTA 250",
        ])
        self.round_pills.set_values([
            "F", "SF", "QF", "R16", "R32", "R64", "R128", "RR",
        ])

        for label, pills in [
                ("Tour", self.tour_pills),
                ("Level", self.level_pills),
                ("Surface", self.surface_pills),
                ("Era", self.era_pills),
                ("Round", self.round_pills)]:
            body_layout.addLayout(self._filter_row(label, pills))

        numeric_row = QHBoxLayout()
        numeric_row.setContentsMargins(0, 0, 0, 0)
        numeric_row.setSpacing(8)
        numeric_row.addWidget(self._label("From"))
        self.year_from = QSpinBox()
        self.year_from.setRange(0, 2030)
        self.year_from.setValue(0)
        self.year_from.setSpecialValueText("—")
        self.year_from.setFixedWidth(72)
        self.year_from.valueChanged.connect(lambda _: self._on_filter_changed())
        numeric_row.addWidget(self.year_from)

        numeric_row.addWidget(self._label("To"))
        self.year_to = QSpinBox()
        self.year_to.setRange(0, 2030)
        self.year_to.setValue(0)
        self.year_to.setSpecialValueText("—")
        self.year_to.setFixedWidth(72)
        self.year_to.valueChanged.connect(lambda _: self._on_filter_changed())
        numeric_row.addWidget(self.year_to)

        numeric_row.addWidget(self._label("Min"))
        self.min_matches = QSpinBox()
        self.min_matches.setRange(0, 500)
        self.min_matches.setSingleStep(5)
        self.min_matches.setValue(20)
        self.min_matches.valueChanged.connect(lambda _: self._on_filter_changed())
        numeric_row.addWidget(self.min_matches)
        numeric_row.addStretch()
        body_layout.addLayout(numeric_row)
        filters_layout.addWidget(self.filters_body)
        root.addWidget(filters)

        self.load_btn = None

        self.detail_title = QLabel("Select a record")
        self.detail_title.setObjectName("recordTitle")
        self.detail_title.setWordWrap(True)
        root.addWidget(self.detail_title)

        status_row = QHBoxLayout()
        status_row.setSpacing(8)
        result_label = QLabel("Leaderboard")
        result_label.setObjectName("sectionLabel")
        status_row.addWidget(result_label)
        status_row.addStretch()
        self.result_status = QLabel("")
        self.result_status.setObjectName("dimLabel")
        self.result_status.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        status_row.addWidget(self.result_status)
        root.addLayout(status_row)

        self.result_table = DataTable(self._RESULT_COLUMNS)
        self.result_table.setObjectName("globalLeaderboard")
        self.result_table.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        self.result_table.horizontalHeader().setStretchLastSection(False)
        self.result_table.horizontalHeader().setSectionResizeMode(3, QHeaderView.Stretch)
        self.result_table.verticalHeader().setDefaultSectionSize(34)
        self.result_table.doubleClicked.connect(self._on_leaderboard_double_clicked)
        root.addWidget(self.result_table, 1)

        # --- Pagination row ---
        page_row = QHBoxLayout()
        page_row.setSpacing(8)
        self.prev_btn = QPushButton("◀ Prev")
        self.prev_btn.setObjectName("accentBtn")
        self.prev_btn.setCursor(Qt.PointingHandCursor)
        self.prev_btn.clicked.connect(self._prev_page)
        self.prev_btn.setEnabled(False)
        page_row.addWidget(self.prev_btn)

        self.page_label = QLabel("")
        self.page_label.setObjectName("dimLabel")
        page_row.addWidget(self.page_label)

        self.next_btn = QPushButton("Next ▶")
        self.next_btn.setObjectName("accentBtn")
        self.next_btn.setCursor(Qt.PointingHandCursor)
        self.next_btn.clicked.connect(self._next_page)
        self.next_btn.setEnabled(False)
        page_row.addWidget(self.next_btn)
        page_row.addStretch()
        root.addLayout(page_row)

    def _page_stylesheet(self):
        return f"""
        QFrame#globalFilters, QFrame#recordListPanel {{
            background-color: {COLORS['bg_secondary']};
            border: 1px solid {COLORS['border']};
            border-radius: 8px;
        }}
        QLabel#globalSummary {{
            color: {COLORS['accent']};
            font-size: {FONTS['size_lg']}pt;
            font-weight: 800;
            background: transparent;
        }}
        QLabel#recordTitle {{
            color: {COLORS['text']};
            font-size: {FONTS['size_xl']}pt;
            font-weight: 800;
            background: transparent;
        }}
        QLabel#recordMeta {{
            color: {COLORS['accent']};
            font-size: {FONTS['size_sm']}pt;
            font-weight: 700;
            background: transparent;
        }}
        QListWidget#globalStatList {{
            background-color: transparent;
            border: none;
            outline: none;
        }}
        QListWidget#globalStatList::item {{
            color: {COLORS['text_dim']};
            padding: 10px 10px;
            border-bottom: 1px solid {COLORS['border']};
        }}
        QListWidget#globalStatList::item:hover {{
            background-color: {COLORS['bg_hover']};
            color: {COLORS['text']};
        }}
        QListWidget#globalStatList::item:selected {{
            background-color: {COLORS['accent_dim']};
            color: {COLORS['accent']};
            border-left: 3px solid {COLORS['accent']};
        }}
        QTableWidget#globalLeaderboard {{
            border: 1px solid {COLORS['border']};
            border-radius: 8px;
        }}
        """

    def _filter_pills(self, values):
        pills = MultiPillButtonGroup(values)
        pills.changed.connect(lambda _: self._on_filter_changed())
        return pills

    def _filter_row(self, label, widget):
        row = QHBoxLayout()
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(8)
        label_widget = self._label(label)
        label_widget.setFixedWidth(64)
        row.addWidget(label_widget)
        row.addWidget(widget, 1)
        return row

    def _label(self, text):
        label = QLabel(text + ":")
        label.setObjectName("dimLabel")
        return label

    def records_panel(self):
        return self._records_panel

    def _toggle_filters(self):
        visible = self.filters_body.isVisible()
        self.filters_body.setVisible(not visible)
        self.filter_toggle.setText("Show filters" if visible else "Hide filters")

    def _on_category_changed(self, category):
        self._category = category
        self._apply_filters()

    def _on_filter_changed(self):
        self._schedule_compute()

    def _apply_filters(self):
        query = self.search_bar.text().lower() if hasattr(self, "search_bar") else ""
        active_categories = CATEGORY_GROUPS.get(self._category)
        current_id = self._current_stat["stat_id"] if self._current_stat else None

        self._filtered_stats = []
        self.stat_list.blockSignals(True)
        self.stat_list.clear()
        target_row = -1
        for stat in STAT_CATALOG:
            haystack = " ".join(str(value) for value in stat.values()).lower()
            if query and query not in haystack:
                continue
            if active_categories and stat["category"] not in active_categories:
                continue
            row = len(self._filtered_stats)
            self._filtered_stats.append(stat)
            item = QListWidgetItem(stat["name"])
            item.setData(Qt.UserRole, stat)
            item.setToolTip(stat["description"])
            self.stat_list.addItem(item)
            if stat["stat_id"] == current_id:
                target_row = row
        if target_row < 0 and self._filtered_stats:
            target_row = 0
        if target_row >= 0:
            self.stat_list.setCurrentRow(target_row)
        self.stat_list.blockSignals(False)

        count = len(self._filtered_stats)
        self.stat_count_label.setText(f"{count} records")
        self.summary_label.setText(f"{count} records - top 50")
        if target_row >= 0:
            self._show_stat(self._filtered_stats[target_row])
        else:
            self._show_stat(None)

    def _on_stat_selected(self, row):
        if 0 <= row < len(self._filtered_stats):
            self._show_stat(self._filtered_stats[row])

    def _show_stat(self, stat):
        if not stat:
            self._current_stat = None
            self.detail_title.setText("No records found")
            self.result_table.populate([])
            self.result_status.setText("")
            return
        self._current_stat = stat
        self.detail_title.setText(stat["name"])
        self.result_table.populate([])
        self._schedule_compute()

    def _current_filters(self):
        return {
            "category": self._category,
            "level": self.level_pills.values(),
            "surface": self.surface_pills.values(),
            "era": self.era_pills.values(),
            "tour": self.tour_pills.values(),
            "round": self.round_pills.values(),
            "min_matches": self.min_matches.value(),
            "min_year": self.year_from.value() or None,
            "max_year": self.year_to.value() or None,
            "milestone": 150,
        }

    def _schedule_compute(self):
        if self._current_stat is None:
            return
        self.result_status.setText("Loading...")
        self._compute_timer.start()

    def _load_current_stat(self):
        if not self._current_stat:
            return
        # Disconnect old worker so stale results are silently discarded
        if self._worker and self._worker.isRunning():
            try:
                self._worker.data_ready.disconnect()
                self._worker.error.disconnect()
            except RuntimeError:
                pass
        self._request_id += 1
        stat_id = self._current_stat["stat_id"]
        if self.load_btn:
            self.load_btn.setEnabled(False)
        self.result_status.setText("Computing...")
        self.result_table.populate([])
        self._streak_meta = []
        self._streak_meta_filtered = []
        self._all_rows = []
        self._current_page = 1
        self.prev_btn.setEnabled(False)
        self.next_btn.setEnabled(False)
        self.page_label.setText("")
        worker = _GlobalStatWorker(
            self.db, stat_id, self._current_filters(),
            request_id=self._request_id, parent=self)
        worker.data_ready.connect(self._on_result_ready)
        worker.error.connect(self._on_result_error)
        self._worker = worker
        worker.start()

    def _on_result_ready(self, stat_id, request_id, result):
        if request_id != self._request_id:
            return  # stale result from an old filter/stat — discard
        if not self._current_stat or stat_id != self._current_stat["stat_id"]:
            return
        if self.load_btn:
            self.load_btn.setEnabled(True)
        rows = result.get("rows") or []
        self._streak_meta = result.get("streaks_meta") or []
        self._all_rows = rows
        self._apply_player_filter()
        note = result.get("note") or ""
        if note:
            self.result_status.setText(note)
        elif rows:
            self.result_status.setText(f"{len(rows)} rows")
        else:
            self.result_status.setText("No results")

    _PAGE_SIZE = 50

    def _apply_player_filter(self):
        """Filter the loaded leaderboard rows by player name (column 1) and show current page."""
        query = self.player_filter_edit.text().strip().lower()
        if query:
            indexed_rows = [
                (i, r) for i, r in enumerate(self._all_rows)
                if query in r[1].lower()
            ]
        else:
            indexed_rows = list(enumerate(self._all_rows))
        filtered_rows = [r for _, r in indexed_rows]
        self._streak_meta_filtered = [
            self._streak_meta[i] if i < len(self._streak_meta) else None
            for i, _ in indexed_rows
        ]

        total = len(filtered_rows)
        total_pages = max(1, (total + self._PAGE_SIZE - 1) // self._PAGE_SIZE)
        self._current_page = max(1, min(self._current_page, total_pages))

        start = (self._current_page - 1) * self._PAGE_SIZE
        page_rows = filtered_rows[start:start + self._PAGE_SIZE]

        # Re-rank with absolute rank number (not resetting per page)
        reranked = []
        for i, r in enumerate(page_rows, start + 1):
            reranked.append([str(i)] + list(r[1:]))
        self.result_table.populate(reranked)

        self.prev_btn.setEnabled(self._current_page > 1)
        self.next_btn.setEnabled(self._current_page < total_pages)
        if total_pages > 1:
            self.page_label.setText(f"Page {self._current_page} of {total_pages}")
        else:
            self.page_label.setText("")

        suffix = f" (filtered from {len(self._all_rows)})" if query else ""
        self.result_status.setText(f"{total} rows{suffix}")

    def _prev_page(self):
        if self._current_page > 1:
            self._current_page -= 1
            self._apply_player_filter()

    def _next_page(self):
        query = self.player_filter_edit.text().strip().lower()
        if query:
            total = sum(1 for r in self._all_rows if query in r[1].lower())
        else:
            total = len(self._all_rows)
        total_pages = max(1, (total + self._PAGE_SIZE - 1) // self._PAGE_SIZE)
        if self._current_page < total_pages:
            self._current_page += 1
            self._apply_player_filter()

    def _on_leaderboard_double_clicked(self, index):
        row = index.row()
        # Map visible row to absolute index in the filtered list (accounting for pagination)
        abs_row = (self._current_page - 1) * self._PAGE_SIZE + row
        meta_list = self._streak_meta_filtered if hasattr(self, "_streak_meta_filtered") and self._streak_meta_filtered else self._streak_meta
        if not meta_list or abs_row >= len(meta_list):
            return
        meta = meta_list[abs_row]
        if not isinstance(meta, dict):
            return
        player = meta["player"]
        value_item = self.result_table.item(row, 2)
        streak_len = value_item.text() if value_item else ""
        detail_item = self.result_table.item(row, 3)
        detail = detail_item.text() if detail_item else ""
        streak_type = meta.get("streak_type", "win")
        if streak_type == "set":
            title = f"{player} — {streak_len} sets consecutivi  ({detail})"
            matches = GlobalStatsEngine(self.db).get_set_streak_matches(
                player=meta["player"],
                start_date=meta["start_date"],
                end_date=meta["end_date"],
                filters=self._current_filters(),
                match_ids=meta.get("match_ids"),
                set_indexes=meta.get("set_indexes"),
            )
            dlg = _SetStreakDetailDialog(title, matches, parent=self)
        else:
            title = f"{player} — {streak_len} wins  ({detail})"
            matches = GlobalStatsEngine(self.db).get_streak_matches(
                player=meta["player"],
                start_date=meta["start_date"],
                end_date=meta["end_date"],
                filters=self._current_filters(),
                group_attr=meta["group_attr"],
                group_value=meta["group_value"],
            )
            dlg = _WinStreakDetailDialog(title, matches, parent=self)
        dlg.exec()

    def _on_result_error(self, stat_id, request_id, message):
        if request_id != self._request_id:
            return  # stale error — discard
        if self._current_stat and stat_id == self._current_stat["stat_id"]:
            if self.load_btn:
                self.load_btn.setEnabled(True)
            self.result_table.populate([])
            self.result_status.setText(f"Error: {message}")


# ---------------------------------------------------------------------------
# Set-streak detail dialog
# ---------------------------------------------------------------------------

class _SetStreakDetailDialog(QDialog):
    """Shows the matches forming a set streak with per-match set scores."""

    def __init__(self, title: str, matches: list[dict], parent=None):
        super().__init__(parent)
        self.setWindowTitle("Set Streak Detail")
        self.resize(720, 480)
        self.setModal(True)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 16, 16, 12)
        layout.setSpacing(10)

        hdr = QLabel(title)
        hdr.setWordWrap(True)
        hdr.setStyleSheet(
            f"font-size: 12pt; font-weight: 700; color: {COLORS['accent']};"
        )
        layout.addWidget(hdr)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)

        inner = QWidget()
        inner_layout = QVBoxLayout(inner)
        inner_layout.setContentsMargins(4, 4, 4, 4)
        inner_layout.setSpacing(2)
        inner.setStyleSheet(f"background-color: {COLORS['bg_card']};")

        if matches:
            total_matches = len(matches)
            for i, m in enumerate(matches, start=1):
                won = bool(m.get("won", 1))
                tourney = m.get("tourney_name") or ""
                rnd = m.get("round") or ""
                opponent = m.get("opponent") or ""
                score_str = m.get("score") or ""
                date = str(m.get("tourney_date") or "")[:10]
                breaks_conceded = int(m.get("breaks_conceded") or 0)

                # Build set score tokens for display
                from ...core.stats_engine import parse_score as _ps
                parsed = _ps(score_str)
                sets_display = ""
                if parsed:
                    set_parts = []
                    streak_set_indexes = m.get("streak_set_indexes") or []
                    streak_set_indexes = set(streak_set_indexes)
                    show_full_match = i == 1 or i == total_matches
                    for set_index, (w_g, l_g, tb) in enumerate(parsed["set_scores"]):
                        if (not show_full_match and streak_set_indexes
                                and set_index not in streak_set_indexes):
                            continue
                        player_won_set = w_g > l_g if won else l_g > w_g
                        pg = w_g if won else l_g
                        og = l_g if won else w_g
                        tb_str = f"({tb})" if tb is not None else ""
                        marker = "✓" if player_won_set else "✗"
                        set_parts.append(f"{marker}{pg}-{og}{tb_str}")
                    sets_display = "  ".join(set_parts)

                result_tag = "W" if won else "L"
                breaks_str = (
                    f"  [{breaks_conceded} break{'s' if breaks_conceded > 1 else ''} subito]"
                    if breaks_conceded and won else ""
                )
                parts = [tourney, rnd, f"vs {opponent}" if opponent else "",
                         sets_display or score_str]
                line_text = f"{i}.  {date}  [{result_tag}]  —  {',  '.join(p for p in parts if p)}{breaks_str}"

                lbl = QLabel(line_text)
                if not won:
                    text_color = "#e05555"
                    bg_color = "#3a1a1a" if i % 2 else "#331515"
                elif breaks_conceded:
                    text_color = "#e09055"
                    bg_color = "transparent" if i % 2 else COLORS.get("bg_secondary", COLORS["bg_card"])
                else:
                    text_color = COLORS["text"]
                    bg_color = "transparent" if i % 2 else COLORS.get("bg_secondary", COLORS["bg_card"])
                lbl.setStyleSheet(
                    f"color: {text_color}; font-size: 9pt;"
                    f" padding: 4px 8px; background-color: {bg_color};"
                )
                lbl.setTextInteractionFlags(Qt.TextSelectableByMouse)
                inner_layout.addWidget(lbl)
        else:
            no_data = QLabel("No match data available.")
            no_data.setStyleSheet(f"color: {COLORS['text_dim']}; padding: 8px;")
            inner_layout.addWidget(no_data)

        inner_layout.addStretch()
        scroll.setWidget(inner)
        layout.addWidget(scroll, 1)

        legend = QLabel("✗ match perso (rosso) · ✓ set vinto · arancione = break subiti")
        legend.setStyleSheet(f"color: {COLORS['text_dim']}; font-size: 8pt;")
        layout.addWidget(legend)

        btns = QDialogButtonBox(QDialogButtonBox.Close)
        btns.rejected.connect(self.reject)
        layout.addWidget(btns)

        self.setStyleSheet(
            f"QDialog {{ background-color: {COLORS['bg_primary']}; }}"
            f"QLabel {{ color: {COLORS['text']}; }}"
            f"QScrollArea {{ background-color: {COLORS['bg_card']};"
            f"  border: 1px solid {COLORS['border']}; border-radius: 6px; }}"
            f"QPushButton {{ background-color: {COLORS['bg_card']};"
            f"  color: {COLORS['text']}; border: 1px solid {COLORS['border']};"
            f"  padding: 5px 14px; border-radius: 4px; }}"
            f"QPushButton:hover {{ background-color: {COLORS['bg_hover']}; }}"
        )


# ---------------------------------------------------------------------------
# Win-streak detail dialog
# ---------------------------------------------------------------------------

class _WinStreakDetailDialog(QDialog):
    """Shows the chronological list of wins in a win streak with per-set markers."""

    def __init__(self, title: str, matches: list[dict], parent=None):
        super().__init__(parent)
        self.setWindowTitle("Win Streak Detail")
        self.resize(720, 480)
        self.setModal(True)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 16, 16, 12)
        layout.setSpacing(10)

        hdr = QLabel(title)
        hdr.setWordWrap(True)
        hdr.setStyleSheet(
            f"font-size: 12pt; font-weight: 700; color: {COLORS['accent']};"
        )
        layout.addWidget(hdr)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)

        inner = QWidget()
        inner_layout = QVBoxLayout(inner)
        inner_layout.setContentsMargins(4, 4, 4, 4)
        inner_layout.setSpacing(2)
        inner.setStyleSheet(f"background-color: {COLORS['bg_card']};")

        if matches:
            from ...core.stats_engine import parse_score as _ps
            for i, m in enumerate(matches, start=1):
                tourney = m.get("tourney_name") or ""
                rnd = m.get("round") or ""
                opponent = m.get("opponent") or ""
                score_str = m.get("score") or ""
                date = str(m.get("tourney_date") or "")[:10]
                breaks_conceded = int(m.get("breaks_conceded") or 0)

                # Parse sets — all matches here are wins (won=True)
                parsed = _ps(score_str)
                sets_display = ""
                if parsed:
                    set_parts = []
                    sets_lost_count = 0
                    for w_g, l_g, tb in parsed["set_scores"]:
                        player_won_set = w_g > l_g   # always winner perspective
                        if not player_won_set:
                            sets_lost_count += 1
                        pg, og = w_g, l_g
                        tb_str = f"({tb})" if tb is not None else ""
                        marker = "✓" if player_won_set else "✗"
                        set_parts.append(f"{marker}{pg}-{og}{tb_str}")
                    sets_display = "  ".join(set_parts)
                else:
                    sets_lost_count = 0

                breaks_str = (
                    f"  [{breaks_conceded} break{'s' if breaks_conceded > 1 else ''} subito]"
                    if breaks_conceded else ""
                )
                parts = [tourney, rnd, f"vs {opponent}" if opponent else "",
                         sets_display or score_str]
                line_text = (
                    f"{i}.  {date}  —  "
                    f"{',  '.join(p for p in parts if p)}{breaks_str}"
                )

                lbl = QLabel(line_text)
                if sets_lost_count:
                    text_color = "#e05555"
                    bg_color = "#3a1a1a" if i % 2 else "#331515"
                elif breaks_conceded:
                    text_color = "#e09055"
                    bg_color = "transparent" if i % 2 else COLORS.get("bg_secondary", COLORS["bg_card"])
                else:
                    text_color = COLORS["text"]
                    bg_color = "transparent" if i % 2 else COLORS.get("bg_secondary", COLORS["bg_card"])
                lbl.setStyleSheet(
                    f"color: {text_color}; font-size: 9pt;"
                    f" padding: 4px 8px; background-color: {bg_color};"
                )
                lbl.setTextInteractionFlags(Qt.TextSelectableByMouse)
                inner_layout.addWidget(lbl)
        else:
            no_data = QLabel("No match data available.")
            no_data.setStyleSheet(f"color: {COLORS['text_dim']}; padding: 8px;")
            inner_layout.addWidget(no_data)

        inner_layout.addStretch()
        scroll.setWidget(inner)
        layout.addWidget(scroll, 1)

        legend = QLabel("✓ set vinto · ✗ set perso (rosso) · arancione = break subiti (senza set persi)")
        legend.setStyleSheet(f"color: {COLORS['text_dim']}; font-size: 8pt;")
        layout.addWidget(legend)

        btns = QDialogButtonBox(QDialogButtonBox.Close)
        btns.rejected.connect(self.reject)
        layout.addWidget(btns)

        self.setStyleSheet(
            f"QDialog {{ background-color: {COLORS['bg_primary']}; }}"
            f"QLabel {{ color: {COLORS['text']}; }}"
            f"QScrollArea {{ background-color: {COLORS['bg_card']};"
            f"  border: 1px solid {COLORS['border']}; border-radius: 6px; }}"
            f"QPushButton {{ background-color: {COLORS['bg_card']};"
            f"  color: {COLORS['text']}; border: 1px solid {COLORS['border']};"
            f"  padding: 5px 14px; border-radius: 4px; }}"
            f"QPushButton:hover {{ background-color: {COLORS['bg_hover']}; }}"
        )

