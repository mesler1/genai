# Write a small terminal roguelike

Write a small roguelike for the terminal in Python 3, standard library only. Keep the game logic in
game.py, apart from the screen, so it can be tested without a terminal; play.py draws it with curses
and reads the keys (arrows or hjkl move, q quits).

game.py must have:

- new_game(seed): a new game. The same seed always gives the same game. The map is 40 columns by
  20 rows, with walls around the edge, some walls inside, the player (@) and an exit (>), and at least
  one path from the player to the exit. A few monsters (m) stand on floor squares.
- move(game, dx, dy): moves the player one square (dx and dy each -1, 0 or 1) unless a wall is
  there, and returns the game. Moving onto a monster removes the monster and costs 1 hit point.
  Reaching the exit wins.
- render(game): the map as a list of 20 strings of 40 characters: '#' wall, '.' floor, '@', '>', 'm'.
- The game has the attributes hp (starts at 10), won (True once the player reaches the exit) and
  over (True once won or hp is 0). move does nothing once over is True.

Add tests for the game logic, and say in README.md how to play.
