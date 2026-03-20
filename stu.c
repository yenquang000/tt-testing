// No includes needed, the tracer will add stdio.h
float calculate_average(int scores[], int min_score, int num_scores)
{
    int total = 0;
    int count = 0;
    for (int i = 0; i < num_scores; i++)
    {
        if (scores[i] >= min_score)
        {
            total += (scores[i] - 10);
            count++;
        }
    }
    if (count == 0)
    {
        return 0.0f;
    }
    float avg = (float)total / count;
    return avg;
}

// Main function to run the code
int main()
{
    int test_scores[] = {100, 80, 50, 90, 70};
    int min_val = 65;
    int num = 5;
    float final_avg = calculate_average(test_scores, min_val, num);
    printf("Final Average: %f\\n", final_avg);
    return 0;
}