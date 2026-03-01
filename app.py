from flask import Flask, render_template, request
from flask_socketio import SocketIO, emit, join_room
import random
import string
from enum import Enum
import os

app = Flask(__name__)
app.config['SECRET_KEY'] = os.environ.get('SECRET_KEY', 'blackjack-secret')

# Для Render и других хостингов
socketio = SocketIO(app, async_mode='threading')

games = {}
players = {}
cheat_modes = {}

class GameState(Enum):
    WAITING = "waiting"
    BETTING = "betting"
    PLAYING = "playing"
    HOST_TURN = "host_turn"
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
        if room in games:
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
        del players[sid]

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
    
    players[sid] = {'room': room_id, 'seat': None, 'is_host': True}
    
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
    players[sid] = {'room': room_id, 'seat': seat, 'is_host': False}
    
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
    
    # Проверяем что 2 карты и они одинаковые по значению
    if len(hand.cards) != 2:
        emit('error', {'message': 'Сплит можно только с 2 картами'})
        return
    
    card1_val = hand.cards[0].value
    card2_val = hand.cards[1].value
    
    # Проверяем что значения одинаковые (2-2, K-K, A-A и т.д.)
    if card1_val != card2_val:
        emit('error', {'message': 'Карты должны быть одинакового значения'})
        return
    
    current_bet = game['bets'][seat]
    
    if game['balances'][seat] < current_bet:
        emit('error', {'message': 'Недостаточно средств для сплита'})
        return
    
    # Списываем деньги за вторую руку
    game['balances'][seat] -= current_bet
    
    # Создаем две новые руки
    hand1 = Hand()
    hand1.add(hand.cards[0])
    hand1.add(game['deck'].deal())
    
    hand2 = Hand()
    hand2.add(hand.cards[1])
    hand2.add(game['deck'].deal())
    
    # Сохраняем сплит-руки
    game['split_hands'][seat] = [hand1, hand2]
    game['split_bets'][seat] = current_bet
    game['current_split_hand'][seat] = 0  # Играем сначала первую руку (0)
    
    # Удаляем оригинальную руку
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
    
    # Проверяем есть ли сплит
    if seat in game['split_hands']:
        split_idx = game['current_split_hand'][seat]
        hand = game['split_hands'][seat][split_idx]
        deck = game['deck']
        
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
        
        # Если перебор - переходим к следующей руке или заканчиваем
        if hand.is_bust:
            if split_idx == 0 and len(game['split_hands'][seat]) > 1:
                # Переходим ко второй руке
                game['current_split_hand'][seat] = 1
            else:
                # Все руки сыграны
                game['finished_players'].add(seat)
                next_player(room)
        else:
            broadcast_state(room)
    else:
        # Обычная логика без сплита
        hand = game['hands'][seat]
        deck = game['deck']
        
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
    
    # Проверяем есть ли сплит
    if seat in game['split_hands']:
        split_idx = game['current_split_hand'][seat]
        
        if split_idx == 0 and len(game['split_hands'][seat]) > 1:
            # Переходим ко второй руке
            game['current_split_hand'][seat] = 1
            broadcast_state(room)
        else:
            # Все руки сыграны
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
        
        # Обработка сплит-рук
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
                
            # Обновляем баланс игрока
            game['balances'][seat] += total_bet + total_winnings
            
            # Определяем результат для отображения
            if total_winnings > 0:
                results[seat] = 'win'
            elif total_winnings < 0:
                results[seat] = 'lose'
            else:
                results[seat] = 'push'
            
            continue
        
        # Обычная обработка
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
    
    if len(game['deck'].cards) < 20:
        game['deck'] = Deck()
    
    broadcast_state(room)

@socketio.on('cheat_balance')
def cheat_balance(data):
    sid = request.sid
    if sid not in players:
        return
    
    room = players[sid]['room']
    amount = data.get('amount', 1000)
    
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
    else:
        emit('error', {'message': 'Место пустое'})

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
        
        # Добавляем данные о сплите если есть (для всех, включая хоста)
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
        
        # Добавляем обычные руки других игроков
        for s, hand in game['hands'].items():
            other_hands[s] = {
                'cards': [c.to_dict() for c in hand.cards],
                'value': hand.value,
                'count': len(hand.cards)
            }
        
        # ДОБАВЛЯЕМ: Сплит-руки других игроков для отображения у хоста и других игроков
        for s in game['split_hands']:
            if s != seat:  # Не добавляем свои сплит-руки в other_hands
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
                    # Для совместимости показываем активную руку как основную
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
        
        emit('game_state', data, room=sid)

if __name__ == '__main__':
    socketio.run(app, host='0.0.0.0', port=10000, allow_unsafe_werkzeug=True)
