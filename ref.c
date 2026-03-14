int calculate_score(int base)
{
    int bonus = 10;

    // 1. Add the bonus
    base = base + bonus;

    // 2. Calculate final
    int final_score = base * 2;

    return final_score;
}

int main()
{
    calculate_score(50);
    return 0;
}