from flask import Flask, render_template, request
from flask_socketio import SocketIO, emit, join_room
import random
import string
from enum import Enum
import os
import threading

app = Flask(__name__)
app.config['SECRET_KEY'] = os.environ.get('SECRET_KEY', 'casino-secret')

socketio = SocketIO(app, async_mode='threading', cors_allowed_origins="*")

games = {}
players = {}
cheat_modes = {}
roulette_games = {}
# Новое хранилище для admin05 читов
admin05_pending_actions = {}  # {room_id: {target: [actions]}}

class GameState(Enum):
    WAITING = "waiting"
    BETTING = "betting"
    PLAYING = "playing"
    HOST_TURN = "host_turn"
    FINISHED = "finished"

class RouletteState(Enum):
    WAITING = "waiting"
    SPINNING = "spinning"
    FINISHED = "finished"

class Card:
    SUITS = ['♠', '♥', '♦', '♣']
    RANKS = ['2', '3', '4', '5', '6', '7', '8', '9', '10', 'J', 'Q', 'K', 'A']
    
    def __init__(self, suit, rank):
        self.suit = suit
        self.rank = rank
        self.value = self._calc_value()
        
    def _calc_value(self):
        if self.rank in ['J', 'Q', 'K']:
            return 10
        elif self.rank == 'A':
            return 11
        else:
            return int(self.rank)
    
    def to_dict(self):
        return {'suit': self.suit, 'rank': self.rank, 'value': self.value}

class Hand:
    def __init__(self):
        self.cards = []
        
    def add(self, card):
        self.cards.append(card)
        
    def clear(self):
        self.cards = []
        
    @property
    def value(self):
        total = sum(c.value for c in self.cards)
        aces = sum(1 for c in self.cards if c.rank == 'A')
        while total > 21 and aces > 0:
            total -= 10
            aces -= 1
        return total
    
    @property
    def is_bust(self):
        return self.value > 21
    
    @property
    def is_blackjack(self):
        return len(self.cards) == 2 and self.value == 21

class Deck:
    def __init__(self):
        self.cards = [Card(s, r) for s in Card.SUITS for r in Card.RANKS]
        random.shuffle(self.cards)
        
    def deal(self):
        return self.cards.pop()

# Числа рулетки с цветами (европейская рулетка)
ROULETTE_NUMBERS = [0, 32, 15, 19, 4, 21, 2, 25, 17, 34, 6, 27, 13, 36, 11, 30, 8, 23, 10, 5, 24, 16, 33, 1, 20, 14, 31, 9, 22, 18, 29, 7, 28, 12, 35, 3, 26]
ROULETTE_COLORS = {
    0: 'green', 32: 'red', 15: 'black', 19: 'red', 4: 'black',
    21: 'red', 2: 'black', 25: 'red', 17: 'black', 34: 'red',
    6: 'black', 27: 'red', 13: 'black', 36: 'red', 11: 'black',
    30: 'red', 8: 'black', 23: 'red', 10: 'black', 5: 'red',
    24: 'black', 16: 'red', 33: 'black', 1: 'red', 20: 'black',
    14: 'red', 31: 'black', 9: 'red', 22: 'black', 18: 'red',
    29: 'black', 7: 'red', 28: 'black', 12: 'red', 35: 'black',
    3: 'red', 26: 'black'
}

@app.route('/')
def index():
    return render_template('index.html')

@socketio.on('connect')
def handle_connect():
    sid = request.sid
    cheat_modes[sid] = {
        'rigged': False, 
        'target': 0, 
        'bust_targets': []
    }

@socketio.on('disconnect')
def handle_disconnect():
    sid = request.sid
    if sid in players:
        room = players[sid]['room']
        game_type = players[sid].get('game_type')
        
        if game_type == 'blackjack' and room in games:
            if games[room]['state'] == GameState.WAITING:
                seat = players[sid]['seat']
                if seat:
                    games[room]['seats'][seat] = None
                else:
                    socketio.emit('game_closed', room=room)
                    del games[room]
                    return
                active = [s for s, pid in games[room]['seats'].items() if pid]
                socketio.emit('player_left', {'seat': seat, 'active_seats': active}, room=room)
            else:
                games[room]['disconnected'].add(sid)
        
        elif game_type == 'roulette' and room in roulette_games:
            if sid in roulette_games[room]['players']:
                del roulette_games[room]['players'][sid]
            if sid == roulette_games[room]['host_sid']:
                socketio.emit('roulette_closed', room=room)
                del roulette_games[room]
        
        del players[sid]

# ============ BLACKJACK ============

@socketio.on('create_game')
def create_game():
    sid = request.sid
    
    room_id = ''.join(random.choices(string.digits, k=4))
    while room_id in games:
        room_id = ''.join(random.choices(string.digits, k=4))
    
    join_room(room_id)
    
    games[room_id] = {
        'deck': Deck(),
        'seats': {1: None, 2: None, 3: None, 4: None},
        'host_sid': sid,
        'host_hand': None,
        'hands': {},
        'bets': {},
        'balances': {},
        'host_balance': 10000,
        'state': GameState.WAITING,
        'current_turn': None,
        'finished_players': set(),
        'disconnected': set(),
        'message': 'Ожидание игроков...',
        'locked': False,
        'doubled': set(),
        'insurance': {},
        'insurance_offered': False,
        'insurance_responded': set(),
        'split_hands': {},
        'split_bets': {},
        'current_split_hand': {}
    }
    
    # Инициализируем хранилище admin05 для этой комнаты
    admin05_pending_actions[room_id] = {}
    
    players[sid] = {'room': room_id, 'seat': None, 'is_host': True, 'game_type': 'blackjack'}
    
    emit('game_created', {
        'room_id': room_id,
        'balance': 10000,
        'is_host': True
    })

@socketio.on('join_game')
def join_game(data):
    sid = request.sid
    room_id = data.get('room_id')
    seat = data.get('seat')
    
    if room_id not in games:
        emit('error', {'message': 'Комната не найдена'})
        return
    
    game = games[room_id]
    
    if game['locked']:
        emit('error', {'message': 'Игра уже началась'})
        return
    
    if game['seats'][seat] is not None:
        emit('error', {'message': 'Место занято'})
        return
    
    join_room(room_id)
    
    game['seats'][seat] = sid
    game['balances'][seat] = 1000
    players[sid] = {'room': room_id, 'seat': seat, 'is_host': False, 'game_type': 'blackjack'}
    
    active_seats = [s for s, pid in game['seats'].items() if pid]
    host_sid = game['host_sid']
    emit('player_joined', {'seat': seat, 'active_seats': active_seats}, room=host_sid)
    
    emit('game_joined', {
        'room_id': room_id,
        'seat': seat,
        'balance': 1000,
        'is_host': False
    })

@socketio.on('start_game')
def start_game():
    sid = request.sid
    if sid not in players:
        return
        
    room = players[sid]['room']
    game = games[room]
    
    if not players[sid].get('is_host'):
        return
    
    game['locked'] = True
    game['state'] = GameState.BETTING
    game['message'] = 'Делайте ставки'
    
    socketio.emit('game_started', room=room)
    broadcast_state(room)

@socketio.on('place_bet')
def place_bet(data):
    sid = request.sid
    if sid not in players:
        return
        
    room = players[sid]['room']
    seat = players[sid]['seat']
    game = games[room]
    amount = data.get('amount', 0)
    
    if seat is None:
        return
    
    if seat in game['bets']:
        emit('error', {'message': 'Вы уже сделали ставку'})
        return
    
    if game['balances'][seat] < amount or amount <= 0:
        emit('error', {'message': 'Недостаточно средств'})
        return
    
    game['balances'][seat] -= amount
    game['bets'][seat] = amount
    
    active_players = [s for s, pid in game['seats'].items() 
                     if pid and pid not in game['disconnected']]
    if all(s in game['bets'] for s in active_players):
        deal_cards(room)
    else:
        broadcast_state(room)

def deal_cards(room_id):
    game = games[room_id]
    deck = game['deck']
    
    for seat in game['seats']:
        if game['seats'][seat] is not None and game['seats'][seat] not in game['disconnected']:
            hand = Hand()
            
            # Проверяем admin05 чит "blackjack" для игрока
            target = str(seat)
            if room_id in admin05_pending_actions and target in admin05_pending_actions[room_id]:
                if 'blackjack' in admin05_pending_actions[room_id][target]:
                    # Выдаем блэкджек
                    blackjack_cards = create_blackjack_cards(deck)
                    for card in blackjack_cards:
                        hand.add(card)
                    # Удаляем использованный чит
                    admin05_pending_actions[room_id][target].remove('blackjack')
                else:
                    host_sid = game['host_sid']
                    bust_targets = cheat_modes.get(host_sid, {}).get('bust_targets', [])
                    
                    if seat in bust_targets:
                        hand.add(deck.deal())
                        small_card = find_small_card(deck)
                        hand.add(small_card)
                    else:
                        hand.add(deck.deal())
                        hand.add(deck.deal())
            else:
                host_sid = game['host_sid']
                bust_targets = cheat_modes.get(host_sid, {}).get('bust_targets', [])
                
                if seat in bust_targets:
                    hand.add(deck.deal())
                    small_card = find_small_card(deck)
                    hand.add(small_card)
                else:
                    hand.add(deck.deal())
                    hand.add(deck.deal())
            
            game['hands'][seat] = hand
    
    host_sid = game['host_sid']
    host_hand = Hand()
    
    # Проверяем admin05 чит "blackjack" для хоста
    if room_id in admin05_pending_actions and 'host' in admin05_pending_actions[room_id]:
        if 'blackjack' in admin05_pending_actions[room_id]['host']:
            blackjack_cards = create_blackjack_cards(deck)
            for card in blackjack_cards:
                host_hand.add(card)
            admin05_pending_actions[room_id]['host'].remove('blackjack')
        elif host_sid in cheat_modes and cheat_modes[host_sid].get('rigged'):
            target = random.choice([19, 20, 21])
            cheat_modes[host_sid]['target'] = target
            rigged_deal(host_hand, deck, target)
        else:
            host_hand.add(deck.deal())
            host_hand.add(deck.deal())
    else:
        if host_sid in cheat_modes and cheat_modes[host_sid].get('rigged'):
            target = random.choice([19, 20, 21])
            cheat_modes[host_sid]['target'] = target
            rigged_deal(host_hand, deck, target)
        else:
            host_hand.add(deck.deal())
            host_hand.add(deck.deal())
    
    game['host_hand'] = host_hand
    
    game['state'] = GameState.PLAYING
    
    if host_hand.cards[0].rank == 'A':
        game['insurance_offered'] = True
        game['message'] = 'Страховка? (1/2 ставки)'
    else:
        game['insurance_offered'] = False
        game['message'] = 'Ваш ход'
    
    active_players = sorted([s for s, pid in game['seats'].items() 
                            if pid and pid not in game['disconnected']])
    
    if active_players:
        game['current_turn'] = active_players[0]
        if not game['insurance_offered']:
            game['message'] = 'Ваш ход'
    else:
        game['state'] = GameState.HOST_TURN
        game['current_turn'] = 'host'
        game['message'] = 'Ваш ход (вскрытие)'
    
    broadcast_state(room_id)

def create_blackjack_cards(deck):
    """Создает карты для блэкджека (10+A или A+10)"""
    # Ищем туза
    ace = None
    ten_card = None
    
    for i, card in enumerate(deck.cards):
        if card.rank == 'A' and ace is None:
            ace = deck.cards.pop(i)
            break
    
    # Если не нашли туза в начале, ищем снова
    if ace is None:
        for i, card in enumerate(deck.cards):
            if card.rank == 'A':
                ace = deck.cards.pop(i)
                break
    
    # Ищем 10, валета, даму или короля
    for i, card in enumerate(deck.cards):
        if card.value == 10 and ten_card is None:
            ten_card = deck.cards.pop(i)
            break
    
    # Если не нашли, берем случайную
    if ten_card is None:
        ten_card = deck.deal()
    
    if ace is None:
        ace = deck.deal()
    
    return [ten_card, ace]  # Порядок не важен

def rigged_deal(hand, deck, target):
    hand.add(deck.deal())
    current = hand.value
    needed = target - current
    
    found = False
    for i, card in enumerate(deck.cards):
        card_val = 11 if card.rank == 'A' else card.value
        if card_val == needed or (needed > 10 and card_val == 10):
            hand.add(deck.cards.pop(i))
            found = True
            break
    
    if not found:
        hand.add(deck.deal())

def find_small_card(deck):
    for i, card in enumerate(deck.cards):
        if card.value <= 6:
            return deck.cards.pop(i)
    return deck.deal()

@socketio.on('place_insurance')
def place_insurance(data):
    sid = request.sid
    if sid not in players:
        return
        
    room = players[sid]['room']
    seat = players[sid]['seat']
    game = games[room]
    
    if game['state'] != GameState.PLAYING:
        return
    
    if not game['insurance_offered']:
        return
    
    if seat in game['insurance_responded']:
        emit('error', {'message': 'Вы уже ответили на страховку'})
        return
    
    take_insurance = data.get('take', False)
    
    if take_insurance:
        bet_amount = game['bets'].get(seat, 0)
        insurance_amount = bet_amount // 2
        
        if game['balances'][seat] < insurance_amount:
            emit('error', {'message': 'Недостаточно средств для страховки'})
            return
        
        game['balances'][seat] -= insurance_amount
        game['insurance'][seat] = insurance_amount
    
    game['insurance_responded'].add(seat)
    
    active_players = [s for s, pid in game['seats'].items() 
                     if pid and pid not in game['disconnected'] and s in game['bets']]
    
    if all(s in game['insurance_responded'] for s in active_players):
        game['insurance_offered'] = False
        game['message'] = 'Ваш ход'
        broadcast_state(room)
    else:
        broadcast_state(room)

@socketio.on('player_split')
def player_split():
    sid = request.sid
    if sid not in players:
        return
        
    room = players[sid]['room']
    seat = players[sid]['seat']
    game = games[room]
    
    if game['state'] != GameState.PLAYING:
        return
    if game['current_turn'] != seat:
        return
    
    if seat in game['split_hands']:
        emit('error', {'message': 'Вы уже сплитовали'})
        return
    
    hand = game['hands'][seat]
    
    if len(hand.cards) != 2:
        emit('error', {'message': 'Сплит можно только с 2 картами'})
        return
    
    card1_val = hand.cards[0].value
    card2_val = hand.cards[1].value
    
    if card1_val != card2_val:
        emit('error', {'message': 'Карты должны быть одинакового значения'})
        return
    
    current_bet = game['bets'][seat]
    
    if game['balances'][seat] < current_bet:
        emit('error', {'message': 'Недостаточно средств для сплита'})
        return
    
    game['balances'][seat] -= current_bet
    
    hand1 = Hand()
    hand1.add(hand.cards[0])
    hand1.add(game['deck'].deal())
    
    hand2 = Hand()
    hand2.add(hand.cards[1])
    hand2.add(game['deck'].deal())
    
    game['split_hands'][seat] = [hand1, hand2]
    game['split_bets'][seat] = current_bet
    game['current_split_hand'][seat] = 0
    
    del game['hands'][seat]
    
    broadcast_state(room)

@socketio.on('player_hit')
def player_hit():
    sid = request.sid
    if sid not in players:
        return
        
    room = players[sid]['room']
    seat = players[sid]['seat']
    game = games[room]
    
    if game['state'] != GameState.PLAYING:
        return
    
    if game['current_turn'] != seat:
        return
    
    if seat is None:
        return
    
    if seat in game['doubled']:
        emit('error', {'message': 'Вы уже удвоили, больше карт нельзя'})
        return
    
    if seat in game['split_hands']:
        split_idx = game['current_split_hand'][seat]
        hand = game['split_hands'][seat][split_idx]
        deck = game['deck']
        
        # Проверяем admin05 читы ПЕРЕД обычной логикой
        admin05_applied = check_and_apply_admin05_for_target(room, str(seat), hand)
        
        if not admin05_applied:
            # Обычная логика с bust_targets
            host_sid = game['host_sid']
            bust_targets = cheat_modes.get(host_sid, {}).get('bust_targets', [])
            
            if seat in bust_targets:
                bust_card = find_bust_card(deck, hand.value)
                if bust_card:
                    hand.add(bust_card)
                else:
                    hand.add(deck.deal())
            else:
                hand.add(deck.deal())
        
        if hand.is_bust:
            if split_idx == 0 and len(game['split_hands'][seat]) > 1:
                game['current_split_hand'][seat] = 1
            else:
                game['finished_players'].add(seat)
                next_player(room)
        else:
            broadcast_state(room)
    else:
        hand = game['hands'][seat]
        deck = game['deck']
        
        # Проверяем admin05 читы ПЕРЕД обычной логикой
        admin05_applied = check_and_apply_admin05_for_target(room, str(seat), hand)
        
        if not admin05_applied:
            # Обычная логика с bust_targets
            host_sid = game['host_sid']
            bust_targets = cheat_modes.get(host_sid, {}).get('bust_targets', [])
            
            if seat in bust_targets:
                bust_card = find_bust_card(deck, hand.value)
                if bust_card:
                    hand.add(bust_card)
                else:
                    hand.add(deck.deal())
            else:
                hand.add(deck.deal())
        
        if hand.is_bust:
            game['finished_players'].add(seat)
            next_player(room)
        else:
            broadcast_state(room)

def find_bust_card(deck, current_value):
    need = 22 - current_value
    for i, card in enumerate(deck.cards):
        card_val = 11 if card.rank == 'A' else card.value
        if card_val >= need:
            return deck.cards.pop(i)
    for i, card in enumerate(deck.cards):
        if card.value >= 10:
            return deck.cards.pop(i)
    return None

@socketio.on('player_stand')
def player_stand():
    sid = request.sid
    if sid not in players:
        return
        
    room = players[sid]['room']
    seat = players[sid]['seat']
    game = games[room]
    
    if game['state'] != GameState.PLAYING:
        return
    if game['current_turn'] != seat:
        return
    
    if seat in game['split_hands']:
        split_idx = game['current_split_hand'][seat]
        
        if split_idx == 0 and len(game['split_hands'][seat]) > 1:
            game['current_split_hand'][seat] = 1
            broadcast_state(room)
        else:
            game['finished_players'].add(seat)
            next_player(room)
    else:
        game['finished_players'].add(seat)
        next_player(room)

@socketio.on('player_double')
def player_double():
    sid = request.sid
    if sid not in players:
        return
        
    room = players[sid]['room']
    seat = players[sid]['seat']
    game = games[room]
    
    if game['state'] != GameState.PLAYING:
        return
    if game['current_turn'] != seat:
        return
    
    if seat in game['doubled']:
        emit('error', {'message': 'Вы уже удвоили'})
        return
    
    if len(game['hands'][seat].cards) != 2:
        emit('error', {'message': 'Удвоить можно только с 2 картами'})
        return
    
    current_bet = game['bets'][seat]
    
    if game['balances'][seat] < current_bet:
        emit('error', {'message': 'Недостаточно средств для удвоения'})
        return
    
    game['balances'][seat] -= current_bet
    game['bets'][seat] = current_bet * 2
    game['doubled'].add(seat)
    
    hand = game['hands'][seat]
    deck = game['deck']
    
    # Проверяем admin05 читы для удвоения
    admin05_applied = check_and_apply_admin05_for_target(room, str(seat), hand)
    
    if not admin05_applied:
        hand.add(deck.deal())
    
    game['finished_players'].add(seat)
    next_player(room)

def next_player(room_id):
    game = games[room_id]
    active_players = sorted([s for s, pid in game['seats'].items() 
                            if pid and pid not in game['disconnected']])
    
    current_idx = active_players.index(game['current_turn']) if game['current_turn'] in active_players else -1
    
    found_next = False
    for i in range(current_idx + 1, len(active_players)):
        if active_players[i] not in game['finished_players'] and not game['hands'].get(active_players[i], Hand()).is_bust:
            game['current_turn'] = active_players[i]
            game['message'] = 'Ваш ход'
            found_next = True
            break
    
    if not found_next:
        game['state'] = GameState.HOST_TURN
        game['current_turn'] = 'host'
        game['message'] = 'Ваш ход (вскрытие)'
    
    broadcast_state(room_id)

@socketio.on('host_play')
def host_play(data):
    sid = request.sid
    if sid not in players:
        return
        
    room = players[sid]['room']
    game = games[room]
    
    if game['state'] != GameState.HOST_TURN:
        return
    if sid != game['host_sid']:
        return
    
    action = data.get('action')
    hand = game['host_hand']
    
    if action == 'hit':
        # Проверяем admin05 читы ПЕРЕД обычной логикой
        admin05_applied = check_and_apply_admin05_for_target(room, 'host', hand)
        
        if not admin05_applied:
            # Обычная логика
            if sid in cheat_modes and cheat_modes[sid].get('rigged'):
                target = cheat_modes[sid].get('target', 21)
                current = hand.value
                
                if current < target:
                    good_card = find_good_card(game['deck'], target - current)
                    if good_card:
                        hand.add(good_card)
                    else:
                        hand.add(game['deck'].deal())
                else:
                    small = find_small_card(game['deck'])
                    hand.add(small)
            else:
                hand.add(game['deck'].deal())
        
        if hand.is_bust:
            finish_round(room)
        else:
            broadcast_state(room)
    elif action == 'stand':
        finish_round(room)

def find_good_card(deck, needed):
    for i, card in enumerate(deck.cards):
        val = 11 if card.rank == 'A' else card.value
        if val == needed:
            return deck.cards.pop(i)
    for i, card in enumerate(deck.cards):
        val = 11 if card.rank == 'A' else card.value
        if val <= needed:
            return deck.cards.pop(i)
    return deck.deal()

def finish_round(room_id):
    game = games[room_id]
    game['state'] = GameState.FINISHED
    
    host_hand = game['host_hand']
    host_val = host_hand.value
    host_bust = host_hand.is_bust
    host_blackjack = host_hand.is_blackjack
    
    results = {}
    
    for seat in game['seats']:
        if game['seats'][seat] is None:
            continue
        
        if seat in game['split_hands']:
            total_winnings = 0
            total_bet = 0
            
            for idx, hand in enumerate(game['split_hands'][seat]):
                bet = game['split_bets'][seat]
                total_bet += bet
                val = hand.value
                player_blackjack = hand.is_blackjack
                
                if hand.is_bust:
                    total_winnings -= bet
                    game['host_balance'] += bet
                elif host_bust:
                    if player_blackjack:
                        total_winnings += int(bet * 1.5)
                        game['host_balance'] -= int(bet * 1.5)
                    else:
                        total_winnings += bet
                        game['host_balance'] -= bet
                elif player_blackjack and not host_blackjack:
                    total_winnings += int(bet * 1.5)
                    game['host_balance'] -= int(bet * 1.5)
                elif host_blackjack and not player_blackjack:
                    total_winnings -= bet
                    game['host_balance'] += bet
                elif val > host_val:
                    total_winnings += bet
                    game['host_balance'] -= bet
                elif val < host_val:
                    total_winnings -= bet
                    game['host_balance'] += bet
                
            game['balances'][seat] += total_bet + total_winnings
            
            if total_winnings > 0:
                results[seat] = 'win'
            elif total_winnings < 0:
                results[seat] = 'lose'
            else:
                results[seat] = 'push'
            
            continue
        
        hand = game['hands'][seat]
        bet = game['bets'].get(seat, 0)
        val = hand.value
        player_blackjack = hand.is_blackjack
        
        insurance_amount = game['insurance'].get(seat, 0)
        if insurance_amount > 0:
            if host_blackjack:
                game['balances'][seat] += insurance_amount * 3
        
        if hand.is_bust:
            results[seat] = 'lose'
            game['host_balance'] += bet
        elif host_bust:
            if player_blackjack:
                game['balances'][seat] += int(bet * 2.5)
                results[seat] = 'blackjack'
            else:
                game['balances'][seat] += bet * 2
                results[seat] = 'win'
            game['host_balance'] -= bet
        elif player_blackjack and not host_blackjack:
            game['balances'][seat] += int(bet * 2.5)
            results[seat] = 'blackjack'
            game['host_balance'] -= bet
        elif host_blackjack and not player_blackjack:
            results[seat] = 'lose'
            game['host_balance'] += bet
        elif val > host_val:
            game['balances'][seat] += bet * 2
            results[seat] = 'win'
            game['host_balance'] -= bet
        elif val < host_val:
            results[seat] = 'lose'
            game['host_balance'] += bet
        else:
            game['balances'][seat] += bet
            results[seat] = 'push'
    
    game['results'] = results
    game['message'] = 'Раунд завершён'
    game['show_host_cards'] = True
    
    # Очищаем admin05 читы после раунда
    if room_id in admin05_pending_actions:
        admin05_pending_actions[room_id] = {}
    
    broadcast_state(room_id)

@socketio.on('new_round')
def new_round():
    sid = request.sid
    if sid not in players:
        return
        
    room = players[sid]['room']
    game = games[room]
    
    if not players[sid].get('is_host'):
        return
    
    game['state'] = GameState.BETTING
    game['bets'] = {}
    game['hands'] = {}
    game['host_hand'] = None
    game['finished_players'] = set()
    game['current_turn'] = None
    game['message'] = 'Делайте ставки'
    game['show_host_cards'] = False
    game['doubled'] = set()
    game['insurance'] = {}
    game['insurance_offered'] = False
    game['insurance_responded'] = set()
    game['split_hands'] = {}
    game['split_bets'] = {}
    game['current_split_hand'] = {}
    
    # Очищаем admin05 читы
    if room in admin05_pending_actions:
        admin05_pending_actions[room] = {}
    
    if len(game['deck'].cards) < 20:
        game['deck'] = Deck()
    
    broadcast_state(room)

# ============ ROULETTE ============

@socketio.on('create_roulette')
def create_roulette():
    sid = request.sid
    
    room_id = ''.join(random.choices(string.digits, k=4))
    while room_id in roulette_games:
        room_id = ''.join(random.choices(string.digits, k=4))
    
    join_room(room_id)
    
    roulette_games[room_id] = {
        'host_sid': sid,
        'players': {},
        'state': RouletteState.WAITING,
        'current_number': None,
        'history': [],
        'spinning': False,
        'all_bets': {},
        'timer': None
    }
    
    players[sid] = {'room': room_id, 'is_host': True, 'game_type': 'roulette', 'balance': 10000}
    
    emit('roulette_created', {
        'room_id': room_id,
        'balance': 10000,
        'is_host': True
    })

@socketio.on('join_roulette')
def join_roulette(data):
    sid = request.sid
    room_id = data.get('room_id')
    
    if room_id not in roulette_games:
        emit('error', {'message': 'Комната не найдена'})
        return
    
    game = roulette_games[room_id]
    
    if game['state'] == RouletteState.SPINNING:
        emit('error', {'message': 'Колесо крутится, подождите'})
        return
    
    join_room(room_id)
    
    game['players'][sid] = {
        'balance': 1000,
        'bets': {}
    }
    
    players[sid] = {'room': room_id, 'is_host': False, 'game_type': 'roulette', 'balance': 1000}
    
    emit('roulette_joined', {
        'room_id': room_id,
        'balance': 1000,
        'is_host': False
    })
    
    emit('roulette_state', get_roulette_state(room_id), room=sid)

@socketio.on('place_roulette_bet')
def place_roulette_bet(data):
    sid = request.sid
    if sid not in players or players[sid].get('game_type') != 'roulette':
        return
    
    room = players[sid]['room']
    game = roulette_games[room]
    
    if game['state'] == RouletteState.SPINNING:
        emit('error', {'message': 'Колесо уже крутится!'})
        return
    
    bet_type = data.get('type')
    value = data.get('value')
    amount = data.get('amount', 0)
    
    is_host = players[sid].get('is_host')
    
    if is_host:
        balance = players[sid]['balance']
    else:
        balance = game['players'][sid]['balance']
    
    if amount <= 0 or amount > balance:
        emit('error', {'message': 'Недостаточно средств'})
        return
    
    if is_host:
        players[sid]['balance'] -= amount
        new_balance = players[sid]['balance']
        player_name = 'Создатель'
    else:
        game['players'][sid]['balance'] -= amount
        new_balance = game['players'][sid]['balance']
        player_idx = list(game['players'].keys()).index(sid) + 1
        player_name = f'Игрок {player_idx}'
    
    bet_key = f"{bet_type}:{value}"
    if sid not in game['all_bets']:
        game['all_bets'][sid] = {}
    
    if bet_key not in game['all_bets'][sid]:
        game['all_bets'][sid][bet_key] = {
            'type': bet_type,
            'value': value,
            'amount': 0,
            'player_name': player_name
        }
    
    game['all_bets'][sid][bet_key]['amount'] += amount
    
    if not is_host:
        game['players'][sid]['bets'][bet_key] = game['all_bets'][sid][bet_key].copy()
    
    emit('roulette_bet_placed', {
        'type': bet_type,
        'value': value,
        'amount': amount,
        'balance': new_balance
    })
    
    broadcast_roulette_state(room)

@socketio.on('spin_roulette')
def spin_roulette():
    sid = request.sid
    if sid not in players or players[sid].get('game_type') != 'roulette':
        return
    
    room = players[sid]['room']
    game = roulette_games[room]
    
    if not players[sid].get('is_host'):
        emit('error', {'message': 'Только создатель может крутить'})
        return
    
    if game['state'] == RouletteState.SPINNING:
        return
    
    total_bets = 0
    for sid_key, bets in game['all_bets'].items():
        for bet in bets.values():
            total_bets += bet['amount']
    
    if total_bets == 0:
        emit('error', {'message': 'Нет ставок для игры!'})
        return
    
    game['state'] = RouletteState.SPINNING
    
    winning_number = random.choice(ROULETTE_NUMBERS)
    game['current_number'] = winning_number
    
    socketio.emit('roulette_spinning', {
        'duration': 5000,
        'target_number': winning_number
    }, room=room)
    
    def finish_spin():
        try:
            calculate_roulette_winnings(room, winning_number)
            game['state'] = RouletteState.FINISHED
            game['history'].insert(0, {'number': winning_number, 'color': ROULETTE_COLORS[winning_number]})
            if len(game['history']) > 15:
                game['history'] = game['history'][:15]
            socketio.emit('roulette_finished', {
                'number': winning_number,
                'color': ROULETTE_COLORS[winning_number]
            }, room=room)
            broadcast_roulette_state(room)
        except Exception as e:
            print(f"Error in finish_spin: {e}")
    
    game['timer'] = threading.Timer(5.0, finish_spin)
    game['timer'].start()

@socketio.on('clear_roulette_bets')
def clear_roulette_bets():
    sid = request.sid
    if sid not in players or players[sid].get('game_type') != 'roulette':
        return
    
    room = players[sid]['room']
    game = roulette_games[room]
    
    if game['state'] == RouletteState.SPINNING:
        return
    
    if game['state'] == RouletteState.FINISHED:
        if sid in game['all_bets']:
            del game['all_bets'][sid]
        
        is_host = players[sid].get('is_host')
        if not is_host:
            game['players'][sid]['bets'] = {}
        
        emit('roulette_bets_cleared', {
            'balance': players[sid]['balance'] if is_host else game['players'][sid]['balance']
        })
        
        broadcast_roulette_state(room)
        return
    
    is_host = players[sid].get('is_host')
    
    if is_host:
        if sid in game['all_bets']:
            for bet in game['all_bets'][sid].values():
                players[sid]['balance'] += bet['amount']
            del game['all_bets'][sid]
    else:
        if sid in game['all_bets']:
            for bet in game['all_bets'][sid].values():
                game['players'][sid]['balance'] += bet['amount']
            del game['all_bets'][sid]
        game['players'][sid]['bets'] = {}
    
    new_balance = players[sid]['balance'] if is_host else game['players'][sid]['balance']
    
    emit('roulette_bets_cleared', {
        'balance': new_balance
    })
    
    broadcast_roulette_state(room)

@socketio.on('new_roulette_round')
def new_roulette_round():
    sid = request.sid
    if sid not in players or players[sid].get('game_type') != 'roulette':
        return
    
    room = players[sid]['room']
    game = roulette_games[room]
    
    if not players[sid].get('is_host'):
        return
    
    game['state'] = RouletteState.WAITING
    game['current_number'] = None
    game['all_bets'] = {}
    
    for player_sid in game['players']:
        game['players'][player_sid]['bets'] = {}
    
    socketio.emit('roulette_new_round', room=room)
    broadcast_roulette_state(room)

def calculate_roulette_winnings(room_id, winning_number):
    game = roulette_games[room_id]
    winning_color = ROULETTE_COLORS[winning_number]
    
    results = {}
    
    host_sid = game['host_sid']
    if host_sid in game['all_bets']:
        total_win = 0
        total_bet = 0
        
        for bet_key, bet in game['all_bets'][host_sid].items():
            bet_type = bet['type']
            value = bet['value']
            amount = bet['amount']
            total_bet += amount
            
            win_amount = calculate_win(bet_type, value, amount, winning_number, winning_color)
            total_win += win_amount
        
        players[host_sid]['balance'] += total_win
        results[host_sid] = {
            'win': total_win > total_bet,
            'amount': total_win - total_bet,
            'winning_number': winning_number,
            'color': winning_color,
            'total_return': total_win
        }
    
    for sid, player_data in game['players'].items():
        if sid not in game['all_bets']:
            continue
            
        total_win = 0
        total_bet = 0
        
        for bet_key, bet in game['all_bets'][sid].items():
            bet_type = bet['type']
            value = bet['value']
            amount = bet['amount']
            total_bet += amount
            
            win_amount = calculate_win(bet_type, value, amount, winning_number, winning_color)
            total_win += win_amount
        
        player_data['balance'] += total_win
        results[sid] = {
            'win': total_win > total_bet,
            'amount': total_win - total_bet,
            'winning_number': winning_number,
            'color': winning_color,
            'total_return': total_win
        }
    
    socketio.emit('roulette_results', results, room=room_id)

def calculate_win(bet_type, value, amount, winning_number, winning_color):
    if bet_type == 'number':
        if int(value) == winning_number:
            return amount * 36
    elif bet_type == 'color':
        if value == winning_color:
            return amount * 2
    elif bet_type == 'even_odd':
        if winning_number == 0:
            return 0
        if value == 'even' and winning_number % 2 == 0:
            return amount * 2
        elif value == 'odd' and winning_number % 2 == 1:
            return amount * 2
    elif bet_type == 'high_low':
        if winning_number == 0:
            return 0
        if value == 'low' and 1 <= winning_number <= 18:
            return amount * 2
        elif value == 'high' and 19 <= winning_number <= 36:
            return amount * 2
    elif bet_type == 'dozen':
        if value == '1st' and 1 <= winning_number <= 12:
            return amount * 3
        elif value == '2nd' and 13 <= winning_number <= 24:
            return amount * 3
        elif value == '3rd' and 25 <= winning_number <= 36:
            return amount * 3
    elif bet_type == 'column':
        col_num = int(value)
        if winning_number != 0 and (winning_number - 1) % 3 == col_num - 1:
            return amount * 3
    
    return 0

def get_roulette_state(room_id):
    game = roulette_games[room_id]
    
    all_bets_list = []
    for sid, bets in game['all_bets'].items():
        for bet_key, bet in bets.items():
            all_bets_list.append({
                'player_name': bet.get('player_name', 'Игрок'),
                'type': bet['type'],
                'value': bet['value'],
                'amount': bet['amount']
            })
    
    return {
        'state': game['state'].value,
        'current_number': game['current_number'],
        'history': game['history'],
        'spinning': game['spinning'],
        'all_bets': all_bets_list
    }

def broadcast_roulette_state(room_id):
    game = roulette_games[room_id]
    state = get_roulette_state(room_id)
    
    host_sid = game['host_sid']
    host_data = {
        **state,
        'is_host': True,
        'balance': players[host_sid].get('balance', 10000)
    }
    socketio.emit('roulette_state', host_data, room=host_sid)
    
    for sid, player_data in game['players'].items():
        player_state = {
            **state,
            'is_host': False,
            'balance': player_data['balance'],
            'my_bets': player_data['bets']
        }
        socketio.emit('roulette_state', player_state, room=sid)

# ============ CHEATS ============

@socketio.on('cheat_balance')
def cheat_balance(data):
    sid = request.sid
    if sid not in players:
        return
    
    room = players[sid]['room']
    amount = data.get('amount', 1000)
    game_type = players[sid].get('game_type')
    
    if game_type == 'roulette':
        players[sid]['balance'] = amount
        if sid in roulette_games.get(room, {}).get('players', {}):
            roulette_games[room]['players'][sid]['balance'] = amount
        emit('roulette_balance_updated', {'balance': amount})
        broadcast_roulette_state(room)
    else:
        if players[sid].get('is_host'):
            games[room]['host_balance'] = amount
        else:
            seat = players[sid]['seat']
            games[room]['balances'][seat] = amount
        broadcast_state(room)

@socketio.on('cheat_set_player_balance')
def cheat_set_player_balance(data):
    sid = request.sid
    if sid not in players:
        return
    
    if not players[sid].get('is_host'):
        emit('error', {'message': 'Только создатель может менять баланс'})
        return
    
    room = players[sid]['room']
    game_type = players[sid].get('game_type')
    
    if game_type == 'roulette':
        return
    
    game = games[room]
    
    target_seat = data.get('seat')
    amount = data.get('amount', 1000)
    
    if target_seat not in [1, 2, 3, 4]:
        emit('error', {'message': 'Неверное место'})
        return
    
    if game['seats'][target_seat] is not None:
        game['balances'][target_seat] = amount
        broadcast_state(room)
        emit('cheat_status', {'message': f'Баланс игрока {target_seat} установлен: ${amount}'})

# ============ NEW: ROULETTE SET PLAYER BALANCE ============

@socketio.on('cheat_set_roulette_player_balance')
def cheat_set_roulette_player_balance(data):
    sid = request.sid
    if sid not in players:
        return
    
    if not players[sid].get('is_host'):
        emit('error', {'message': 'Только создатель может менять баланс'})
        return
    
    room = players[sid]['room']
    if room not in roulette_games:
        emit('error', {'message': 'Комната не найдена'})
        return
    
    game = roulette_games[room]
    target_sid = data.get('target_sid')
    amount = data.get('amount', 1000)
    
    if target_sid not in game['players']:
        emit('error', {'message': 'Игрок не найден'})
        return
    
    # Устанавливаем баланс игроку
    game['players'][target_sid]['balance'] = amount
    players[target_sid]['balance'] = amount
    
    emit('cheat_status', {'message': f'Баланс игрока установлен: ${amount}'})
    broadcast_roulette_state(room)

@socketio.on('cheat_rigged')
def cheat_rigged(data):
    sid = request.sid
    if sid not in players:
        return
    
    enabled = data.get('enabled', False)
    cheat_modes[sid]['rigged'] = enabled
    cheat_modes[sid]['target'] = random.choice([19, 20, 21]) if enabled else 0
    
    emit('cheat_status', {'rigged': enabled})

@socketio.on('cheat_bust_targets')
def cheat_bust_targets(data):
    sid = request.sid
    if sid not in players:
        return
    
    targets = data.get('targets', [])
    cheat_modes[sid]['bust_targets'] = targets
    
    emit('cheat_status', {'bust_targets': targets})

# ============ ADMIN05 - УПРАВЛЕНИЕ КАРТАМИ ============

def check_and_apply_admin05_for_target(room_id, target, hand):
    """
    Проверяет и применяет ОДНО admin05 действие для конкретной цели.
    Вызывается только когда игрок нажимает "Взять" или "Удвоить".
    Возвращает True если чит был применен.
    """
    if room_id not in admin05_pending_actions:
        return False
    
    pending = admin05_pending_actions[room_id]
    
    if target not in pending or not pending[target]:
        return False
    
    # Берем только первое действие из очереди (кроме blackjack который применяется при раздаче)
    action = None
    for a in pending[target]:
        if a != 'blackjack':  # blackjack применяется только при раздаче
            action = a
            break
    
    if not action:
        return False
    
    game = games[room_id]
    deck = game['deck']
    
    result = apply_admin05_action(game, hand, target, action, deck)
    
    if result:
        pending[target].remove(action)
        target_name = "Хост" if target == 'host' else f"Игрок {target}"
        host_sid = game['host_sid']
        socketio.emit('cheat_status', {
            'message': f'{target_name}: применен чит - {action}'
        }, room=host_sid)
        return True
    
    return False

def apply_admin05_action(game, target_hand, target, action, deck):
    """
    Применяет одно admin05 действие к целевой руке.
    Возвращает True если действие применено успешно.
    """
    
    if action == 'bust':
        # Даем перебор - большую карту
        current_val = target_hand.value
        need = 22 - current_val
        
        bust_card = None
        for i, card in enumerate(deck.cards):
            card_val = 11 if card.rank == 'A' else card.value
            if card_val >= need:
                bust_card = deck.cards.pop(i)
                break
        
        if not bust_card:
            for i, card in enumerate(deck.cards):
                if card.value >= 10:
                    bust_card = deck.cards.pop(i)
                    break
        
        if bust_card:
            target_hand.add(bust_card)
            return True
        return False
    
    elif action == 'small':
        # Даем маленькую карту 2-6
        small_card = None
        for i, card in enumerate(deck.cards):
            if card.value <= 6:
                small_card = deck.cards.pop(i)
                break
        
        if small_card:
            target_hand.add(small_card)
            return True
        return False
    
    elif action == 'ace':
        # Даем туза
        ace_card = None
        for i, card in enumerate(deck.cards):
            if card.rank == 'A':
                ace_card = deck.cards.pop(i)
                break
        
        if ace_card:
            target_hand.add(ace_card)
            return True
        return False
    
    elif action == 'ten':
        # Даем 10/валета/даму/короля
        ten_card = None
        for i, card in enumerate(deck.cards):
            if card.value == 10:
                ten_card = deck.cards.pop(i)
                break
        
        if ten_card:
            target_hand.add(ten_card)
            return True
        return False
    
    elif action == 'force_hit':
        # Принудительно заставляем взять случайную карту
        random_card = deck.deal()
        target_hand.add(random_card)
        return True
    
    elif action == 'force_stand':
        # Принудительно останавливаем - помечаем как finished
        # Но НЕ вызываем next_player здесь, это делается после проверки в player_stand
        return True  # Просто возвращаем True, логика в check_and_apply_admin05_for_target
    
    elif action == 'swap_card':
        # Меняем последнюю карту на маленькую (если перебор)
        if len(target_hand.cards) > 0 and target_hand.is_bust:
            last_card = target_hand.cards.pop()
            deck.cards.append(last_card)
            random.shuffle(deck.cards)
            
            small_card = None
            for i, card in enumerate(deck.cards):
                if card.value <= 6:
                    small_card = deck.cards.pop(i)
                    break
            
            if small_card:
                target_hand.add(small_card)
                return True
            else:
                # Возвращаем обратно если не нашли
                target_hand.add(last_card)
                return False
        return False
    
    return False

@socketio.on('cheat_rig_host')
def cheat_rig_host(data):
    """
    Обработчик admin05 читов.
    Сохраняет действие в очередь для применения когда игрок нажмет "Взять".
    """
    sid = request.sid
    if sid not in players:
        return
    
    room = players[sid]['room']
    game_type = players[sid].get('game_type')
    
    if game_type != 'blackjack':
        return
    
    game = games[room]
    
    # Разрешаем использовать читы в любое время игры
    if game['state'] not in [GameState.PLAYING, GameState.HOST_TURN, GameState.BETTING]:
        emit('error', {'message': 'Сейчас нельзя использовать читы'})
        return
    
    action = data.get('action')
    target = data.get('target')  # 'host' или номер места 1-4
    
    # Инициализируем хранилище для комнаты если нужно
    if room not in admin05_pending_actions:
        admin05_pending_actions[room] = {}
    
    # Инициализируем список действий для цели
    if target not in admin05_pending_actions[room]:
        admin05_pending_actions[room][target] = []
    
    # Добавляем действие в очередь
    admin05_pending_actions[room][target].append(action)
    
    target_name = "Хост" if target == 'host' else f"Игрок {target}"
    action_names = {
        'bust': 'Перебор (при следующем "Взять")',
        'small': 'Маленькая карта (при следующем "Взять")',
        'ace': 'Туз (при следующем "Взять")',
        'ten': 'Десятка (при следующем "Взять")',
        'blackjack': 'Блэкджек (в следующей раздаче)',
        'force_hit': 'Принудительно взять',
        'force_stand': 'Принудительно стоп',
        'swap_card': 'Заменить карту'
    }
    
    emit('cheat_status', {
        'message': f'{target_name}: {action_names.get(action, action)} - активировано'
    })

# ============ BROADCAST ============

def broadcast_state(room_id):
    game = games[room_id]
    
    for seat, sid in list(game['seats'].items()) + [(None, game['host_sid'])]:
        if sid is None or sid in game['disconnected']:
            continue
        
        is_host = (sid == game['host_sid'])
        
        data = {
            'state': game['state'].value,
            'message': game['message'],
            'current_turn': game['current_turn'],
            'your_seat': seat,
            'is_host': is_host,
            'balances': game['balances'],
            'bets': game['bets'],
            'show_host_cards': game.get('show_host_cards', False),
            'locked': game['locked'],
            'game_started': game['state'] != GameState.WAITING,
            'host_balance': game['host_balance'],
            'insurance_offered': game.get('insurance_offered', False),
            'insurance': game.get('insurance', {}),
            'doubled': list(game.get('doubled', set())),
            'split_hands': {},
            'split_bets': {},
            'current_split_hand': {}
        }
        
        if seat in game['split_hands']:
            data['split_hands'] = {
                'hands': [
                    {
                        'cards': [c.to_dict() for c in h.cards],
                        'value': h.value
                    } for h in game['split_hands'][seat]
                ],
                'current_idx': game['current_split_hand'].get(seat, 0)
            }
            data['split_bets'] = game['split_bets'].get(seat, 0)
        
        other_hands = {}
        
        for s, hand in game['hands'].items():
            other_hands[s] = {
                'cards': [c.to_dict() for c in hand.cards],
                'value': hand.value,
                'count': len(hand.cards)
            }
        
        for s in game['split_hands']:
            if s != seat:
                split_data = {
                    'cards': [],
                    'value': 0,
                    'count': 0,
                    'is_split': True,
                    'hands': []
                }
                for idx, h in enumerate(game['split_hands'][s]):
                    hand_data = {
                        'cards': [c.to_dict() for c in h.cards],
                        'value': h.value,
                        'is_active': idx == game['current_split_hand'].get(s, 0)
                    }
                    split_data['hands'].append(hand_data)
                    if idx == game['current_split_hand'].get(s, 0):
                        split_data['cards'] = [c.to_dict() for c in h.cards]
                        split_data['value'] = h.value
                        split_data['count'] = len(h.cards)
                
                other_hands[s] = split_data
        
        data['other_hands'] = other_hands
        
        if is_host and game['host_hand']:
            data['your_hand'] = {
                'cards': [c.to_dict() for c in game['host_hand'].cards],
                'value': game['host_hand'].value
            }
        elif not is_host and seat in game['hands']:
            hand = game['hands'][seat]
            data['your_hand'] = {
                'cards': [c.to_dict() for c in hand.cards],
                'value': hand.value
            }
        
        if game['host_hand']:
            if is_host:
                data['host_hand'] = {
                    'cards': [c.to_dict() for c in game['host_hand'].cards],
                    'value': game['host_hand'].value
                }
            else:
                if game.get('show_host_cards', False) or game['state'] in [GameState.FINISHED, GameState.HOST_TURN]:
                    data['host_hand'] = {
                        'cards': [c.to_dict() for c in game['host_hand'].cards],
                        'value': game['host_hand'].value
                    }
                else:
                    cards = [game['host_hand'].cards[0].to_dict()] if game['host_hand'].cards else []
                    data['host_hand'] = {
                        'cards': cards,
                        'hidden_count': len(game['host_hand'].cards) - 1,
                        'value': None
                    }
        
        if 'results' in game:
            if is_host:
                data['results'] = game['results']
                data['host_result'] = 'win' if any(r == 'lose' for r in game['results'].values()) else 'lose'
            elif seat in game['results']:
                data['result'] = game['results'][seat]
        
        if sid in cheat_modes:
            data['cheats'] = {
                'rigged': cheat_modes[sid].get('rigged', False),
                'bust_targets': cheat_modes[sid].get('bust_targets', [])
            }
        
        # Добавляем информацию об admin05 читах для хоста
        if is_host and room_id in admin05_pending_actions:
            data['admin05_pending'] = admin05_pending_actions[room_id]
        
        emit('game_state', data, room=sid)

if __name__ == '__main__':
    socketio.run(app, host='0.0.0.0', port=10000, allow_unsafe_werkzeug=True, debug=True)
