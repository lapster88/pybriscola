from poker import *
import logging
import random, string
import redis
import signal
import sys
import time
import os


class InputTimeout(Exception):
    pass


class PlayerConsole(Player):
    def __init__(self, id, name, money):
        Player.__init__(self, id, name, money)

    @staticmethod
    def input(timeout_epoch, question=""):
        def timeout_handler():
            raise InputTimeout("Timed out")

        timeout = int(round(timeout_epoch - time.time()))

        signal.signal(signal.SIGALRM, timeout_handler)
        signal.alarm(timeout)

        print("{:.0f} seconds remaining".format(timeout))
        response = raw_input(question)

        signal.alarm(0)

        return response

    def change_cards(self, timeout_epoch):
        """Gives players the opportunity to discard some of their cards.
        Returns a tuple: (remaining cards, discards)."""
        print(str(self))
        print(Card.format_cards(self._cards))

        while True:
            try:
                discard_keys = PlayerConsole.input(
                    timeout_epoch,
                    "Please type a comma separated list of card (1 to 5 from left to right): ")
                if discard_keys:
                    # Convert the string into a list of unique integers
                    discard_keys = [int(card_id.strip()) - 1 for card_id in str(discard_keys).split(",")]
                    if len(discard_keys) > 4:
                        print("You cannot change more than 4 cards")
                        continue
                    # Works out the new card set
                    discards = [self._cards[key] for key in discard_keys]
                    return discard_keys, discards
                return [], []
            except InputTimeout:
                return [], []
            except (ValueError, IndexError):
                print("One or more invalid card id.")

    def bet(self, min_bet, max_bet, opening, timeout_epoch):
        """Bet handling.
        Returns the player bet. -1 to fold (or to skip the bet round during the opening phase)."""
        print(str(self))
        print(str(self._score))

        if max_bet == -1:
            try:
                PlayerConsole.input(timeout_epoch, "Not allowed to open. Press enter to continue.")
            except InputTimeout:
                pass
            finally:
                return -1

        while True:
            message = "Type your bet."
            if min_bet:
                message += " min bet: ${:,.2f}".format(min_bet)
            if max_bet:
                message += " max bet: ${:,.2f}".format(max_bet)
            message += " -1 to " + ("skip opening" if opening else "fold") + ": "

            try:
                bet = PlayerConsole.input(timeout_epoch, message)

                bet = float(bet)

                if bet == -1:
                    return -1
                elif bet < min_bet:
                    raise ValueError
                elif max_bet and bet > max_bet:
                    raise ValueError

                self._money -= bet
                return bet

            except InputTimeout:
                print("Timed out")
                return -1
            except (ValueError, TypeError):
                print("Invalid bet.")

    def send_message(self, message):
        """Sends a message to the client."""
        pass

    def recv_message(self):
        """Receives a message to the client."""
        pass

    def __str__(self):
        return "\n" + "{} ${:,.2f}".format(self.get_name(), self.get_money())


class PlayerClientConsole(PlayerConsole):
    def __init__(self, server, id, name, money):
        PlayerConsole.__init__(self, id=id, name=name, money=money)
        self._server = server

    def set_cards(self, cards, score):
        """Assigns a list of cards to the player"""
        PlayerConsole.set_cards(self, cards, score)
        print(str(self))
        print(str(self._score))

    def change_cards(self, timeout_epoch):
        """Gives players the opportunity to discard some of their cards.
        Returns a list of discarded cards."""
        discard_keys, discards = PlayerConsole.change_cards(self, timeout_epoch)
        self.send_message({'msg_id': 'change-cards', 'cards': discard_keys})
        return discard_keys, discards

    def bet(self, min_bet, max_bet, opening, timeout_epoch):
        """Bet handling.
        Returns the player bet. -1 to fold (or to skip the bet round during the opening phase)."""
        bet = PlayerConsole.bet(self, min_bet, max_bet, opening, timeout_epoch)
        self.send_message({'msg_id': 'bet', 'bet': bet})

    def try_send_message(self, message):
        try:
            self.send_message(message)
            return True
        except ChannelError:
            return False

    def send_message(self, message):
        return self._server.send_message(message)

    def recv_message(self):
        return self._server.recv_message()

    def __str__(self):
        return "\n" + "{} ${:,.2f}".format(self.get_name(), self.get_money())


class GameClientConsole:
    def __init__(self, player):
        self._player = player
    
    def play(self):
        while True:
            message = self._player.recv_message()

            if message["msg_id"] == "disconnect":
                print()
                print("#" * 80)
                print()
                print("DISCONNECTING")
                print()
                print("#" * 80)
                print()
                break

            elif message["msg_id"] == "game-update":
                self._print_game_update(message)
                if message["event"] == "player-action":
                    action_player = message["players"][message["player"]]

                    if action_player["id"] == self._player.get_id():
                        timeout_epoch = time.time() + message["timeout"]

                        try:
                            if message["action"] == "bet":
                                self._player.bet(
                                    min_bet=message["min_bet"],
                                    max_bet=message["max_bet"],
                                    opening=message["opening"],
                                    timeout_epoch=timeout_epoch
                                )
                            elif message["action"] == "change-cards":
                                self._player.change_cards(timeout_epoch)
                        except InputTimeout:
                            print("Time is up!")
                    else:
                        print("Waiting for {}...".format(action_player["name"]))

            elif message["msg_id"] == "set-cards":
                cards = [Card(rank, suit) for rank, suit in message["cards"]]
                score = Score(message["score"]["category"],
                              [Card(rank, suit) for rank, suit in message["score"]["cards"]])
                self._player.set_cards(cards, score)
                print(Card.format_cards(cards))

            elif message["msg_id"] == "ping":
                self._player.send_message({"msg_id": "ping"})

    def _print_player(self, player):
        print("Player '{}'\n Cash: ${:,.2f}\n Bets: ${:,.2f}".format(
            player["name"],
            player["money"],
            player["bet"]))

        score = None

        if "score" in player and player["score"]:
            category = player["score"]["category"]
            cards = [Card(rank, suit) for (rank, suit) in player["score"]["cards"]]
            score = Score(category=category, cards=cards)

        elif player["id"] == self._player.get_id():
            score = self._player.get_score()

        if score:
            print(Card.format_cards(score.get_cards()))
        else:
            print("+-------+" * 5)
            print("| ///// |" * 5)
            print("| ///// |" * 5)
            print("| ///// |" * 5)
            print("| ///// |" * 5)
            print("| ///// |" * 5)
            print("+-------+" * 5)

    def _print_game_update(self, message):
        print()
        print("~" * 45)

        if message["event"] == "new-game":
            print()
            print("#" * 80)
            print()
            print("NEW GAME")
            print()
            print("#" * 80)
            print()

        elif message["event"] == "game-over":
            print()
            print("#" * 80)
            print()
            print("GAME OVER")
            print()
            print("#" * 80)
            print()

        elif message["event"] == "cards-assignment":
            print("NEW HAND")
            print()
            print("~" * 45)
            for player in message["players"]:
                self._print_player(player)

        elif message["event"] == "bet":
            player_name = message["players"][message["player"]]["name"]
            if message["bet_type"] == "raise":
                print("Player '{}' bet ${:,.2f} RAISE".format(player_name, message["bet"], message["bet_type"]))
            else:
                print("Player '{}' {}".format(player_name, message["bet_type"]))

        elif message["event"] == "cards-change":
            player_name = message["players"][message["player"]]["name"]
            print("Player '{}' changed {} cards".format(player_name, message["num_cards"]))

        elif message["event"] == "winner-designation":
            for player in message["players"]:
                self._print_player(player)

            player_name = message["players"][message["player"]]["name"]
            print()
            print("~" * 45)
            print("Player '{}' WON!!!".format(player_name))
            print("~" * 45)
            print()
            print("Waiting...")
        else:
            print()
            print("~" * 45)
            print("Pot: ${:,.2f}".format(message["pot"]))
            print("~" * 45)
            print()
            print("Waiting...")


if __name__ == "__main__":
    logging.basicConfig(level=logging.DEBUG if 'DEBUG' in os.environ else logging.INFO)
    logger = logging.getLogger()

    server_id = "VEGAS"

    player_id = ''.join(random.choice(string.ascii_lowercase) for i in range(5))
    player_name = sys.argv[1]
    player_money = 1000.0

    redis_url = "redis://localhost" if "REDIS_URL" not in os.environ else os.environ["REDIS_URL"]
    redis = redis.from_url(redis_url)

    connection_request = RedisPublisher(redis, "poker5:server:{}".format(server_id))
    connection_request.send_message({
        "msg_id": "connect",
        "player": {
            "id": player_id,
            "name": player_name,
            "money": player_money
        }
    })

    server_channel = Chan(
        RedisMessageReader(redis, "poker5:server-{}:player-{}:R".format(server_id, player_id)),
        RedisMessageWriter(redis, "poker5:server-{}:player-{}:L".format(server_id, player_id)),
    )

    connection_response = server_channel.recv_message(time.time() + 1)
    MessageFormatError.validate_msg_id(connection_response, "connect")

    # host = "localhost" if "POKER5_HOST" not in os.environ else os.environ["POKER5_HOST"]
    # port = 9000 if "POKER5_PORT" not in os.environ else os.environ["POKER5_PORT"]
    # server_address = (host, port)
    #
    # sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    # sock.connect(server_address)
    # logger.info("Connection established with the Poker5 server at {}".format(server_address))
    #
    # server_channel = SocketChannel(sock, server_address)

    player = PlayerClientConsole(
                server_channel,
                id=player_id,
                name=player_name,
                money=1000)

    game = GameClientConsole(player)

    logger.info("Player {} '{}' ${:,.2f} CONNECTED".format(player.get_id(), player.get_name(), player.get_money()))

    try:
        game.play()
    finally:
        logger.info("Poker5 server {}: connection closed".format(server_id))